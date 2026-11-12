"""Baustein 2: Aufbereitung der Rohdokumente zu Abschnitten, Tags, Klingelschildern,
Cluster-Schildern und der Faktentabelle Angebotspositionen.

Inkrementell: Ein Schild wird nur neu erzeugt, wenn sich Inhalt, Prompt- oder Modellversion
oder die Zuordnung zu Kunde/Projekt geändert haben. Ändern sich nur die Rechte, werden die
abgeleiteten Daten ohne LLM-Aufruf umgehängt.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import PurePosixPath

from psycopg.types.json import Jsonb

from gkb.aufbereitung import fakten
from gkb.aufbereitung.abschnitte import zerlege
from gkb.aufbereitung.embeddings import Embedder, als_vector
from gkb.aufbereitung.klingelschild import (
    CLUSTER_PROMPT_VERSION, PROMPT_VERSION, SYSTEM_CLUSTER, ClusterEntwurf, SchildEntwurf, kuerze,
    system_schild, text_ansicht, user_schild,
)
from gkb.aufbereitung.llm import LLM, frage, schaetze_tokens
from gkb.aufbereitung.vokabular import Vokabular, vorbelegen, vorschlagen

log = logging.getLogger(__name__)


def _h(*teile) -> str:
    return hashlib.sha256("|".join(str(t) for t in teile).encode()).hexdigest()[:16]


def _norm_firma(name: str) -> str:
    n = name.lower()
    n = re.sub(r"\b(gmbh|ag|kg|ug|mbh|co|ohg|se|e\.?\s?k\.?|haftungsbeschränkt)\b", " ", n)
    return re.sub(r"[^\w\[\]]+", " ", n).strip()


class Zuordnung:
    """Kunde und Projekt aus dem Ordnerpfad: <Wissensraum>/<Kunde>/<Projekt>/... (Ebenen konfigurierbar)."""

    def __init__(self, conn, pseudo, kunde_ebene: int = 1, projekt_ebene: int = 2):
        self.conn, self.pseudo = conn, pseudo
        self.kunde_ebene, self.projekt_ebene = kunde_ebene, projekt_ebene
        self.kunden = {
            _norm_firma(r["name"]): r for r in conn.execute("SELECT kundennummer, name FROM kunde").fetchall()
        }

    def kunde_nach_name(self, name: str) -> dict | None:
        n = _norm_firma(name)
        if not n:
            return None
        if n in self.kunden:
            return self.kunden[n]
        treffer = [k for key, k in self.kunden.items() if key and (key in n or n in key) and len(key) >= 4]
        return treffer[0] if len(treffer) == 1 else None

    def fuer(self, pfad: str) -> tuple[str, dict | None, dict | None]:
        teile = PurePosixPath(pfad).parts
        wissensraum = teile[0]
        kunde = projekt = None
        if self.kunde_ebene and len(teile) > self.kunde_ebene + 1:
            kunde = self.kunde_nach_name(self.pseudo.pseudonymize(teile[self.kunde_ebene]))
        if self.projekt_ebene and len(teile) > self.projekt_ebene + 1:
            ppfad = "/".join(teile[: self.projekt_ebene + 1])
            name = self.pseudo.pseudonymize(teile[self.projekt_ebene])
            pid = "P-" + _h(ppfad)[:10]
            self.conn.execute(
                """INSERT INTO projekt (id, name, kunde, pfad) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (pfad) DO UPDATE SET name = EXCLUDED.name, kunde = EXCLUDED.kunde""",
                (pid, name, kunde["kundennummer"] if kunde else None, ppfad),
            )
            projekt = {"id": pid, "name": name}
        return wissensraum, kunde, projekt


def aufbereiten(conn, llm: LLM, embedder: Embedder, pseudo, kunde_ebene: int = 1,
                projekt_ebene: int = 2) -> Counter:
    z: Counter = Counter()
    vorbelegen(conn)
    vok = Vokabular(conn)
    zu = Zuordnung(conn, pseudo, kunde_ebene, projekt_ebene)

    # Abgeleitetes zu gelöschten Dokumenten entfernen (Lineage: Schilder, Abschnitte, Fakten)
    weg = conn.execute(
        """DELETE FROM klingelschild k USING rohdokument r
           WHERE k.rohdokument_id = r.id AND r.geloescht RETURNING k.id"""
    ).fetchall()
    conn.execute("DELETE FROM abschnitt a USING rohdokument r WHERE a.rohdokument_id = r.id AND r.geloescht")
    conn.execute("DELETE FROM angebotsposition a USING rohdokument r WHERE a.rohdokument_id = r.id AND r.geloescht")
    conn.execute("DELETE FROM angebot a USING rohdokument r WHERE a.rohdokument_id = r.id AND r.geloescht")
    if weg:
        conn.execute("DELETE FROM lineage WHERE abgeleitet_typ='klingelschild' AND abgeleitet_id = ANY(%s)",
                     ([w["id"] for w in weg],))
    z["geloescht"] = len(weg)
    conn.commit()

    docs = conn.execute(
        """SELECT r.*, b.deep_link AS beleg_link, b.id AS beleg
           FROM rohdokument r LEFT JOIN lieferbeleg b ON b.id = r.beleg_id
           WHERE NOT r.geloescht AND r.markdown_pseudo IS NOT NULL ORDER BY r.id"""
    ).fetchall()
    for doc in docs:
        try:
            z[_einheit(conn, llm, embedder, vok, zu, doc)] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            log.exception("Aufbereitung fehlgeschlagen: Dokument %s", doc["id"])
            z["fehler"] += 1

    z["fassungen_abweichend"] = fakten.abgleich_fassungen(conn)
    conn.commit()
    z.update({f"cluster_{k}": v for k, v in cluster_bauen(conn, llm, embedder).items()})
    conn.commit()
    return z


def _einheit(conn, llm: LLM, embedder: Embedder, vok: Vokabular, zu: Zuordnung, doc: dict) -> str:
    wissensraum, kunde, projekt = zu.fuer(doc["pfad"])
    schluessel = str(doc["id"])
    inhalt_hash = _h(doc["sha256"], PROMPT_VERSION, fakten.PROMPT_VERSION, llm.name, embedder.name,
                     kunde and kunde["kundennummer"], projekt and projekt["id"])
    alt = conn.execute(
        "SELECT * FROM klingelschild WHERE ebene='einheit' AND schluessel=%s", (schluessel,)
    ).fetchone()
    partition = doc["rechte_partition"]

    if alt and alt["eingabe_hash"] == inhalt_hash:
        status = "unveraendert"
        if alt["rechte_partition"] != partition:
            for tabelle in ("abschnitt", "angebot", "angebotsposition"):
                conn.execute(f"UPDATE {tabelle} SET rechte_partition=%s WHERE rohdokument_id=%s",
                             (partition, doc["id"]))
            status = "rechte_aktualisiert"
        daten = alt["daten"]
        daten["rechte_partition"] = partition
        tags = _tags(vok, daten["roh"], wissensraum, kunde, projekt)
        daten["tags"] = tags
        conn.execute("UPDATE klingelschild SET rechte_partition=%s, tags=%s, daten=%s WHERE id=%s",
                     (partition, Jsonb(tags), Jsonb(daten), alt["id"]))
        return status

    # 1. Abschnitte + Embeddings (Stufe 3 und Grundlage für klassisches RAG)
    teile = zerlege(doc["markdown_pseudo"])
    conn.execute("DELETE FROM abschnitt WHERE rohdokument_id=%s", (doc["id"],))
    vektoren = embedder.embed([f"{a.ueberschrift}\n{a.text}" for a in teile]) if teile else []
    for nr, (a, v) in enumerate(zip(teile, vektoren)):
        conn.execute(
            """INSERT INTO abschnitt (rohdokument_id, nr, ueberschrift, text, tokens, embedding, rechte_partition)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (doc["id"], nr, a.ueberschrift, a.text, a.tokens, als_vector(v), partition),
        )

    # 2. Klingelschild
    e = frage(llm, system_schild(vok.werte("dokumenttyp")),
              user_schild(doc["titel"], kuerze(doc["markdown_pseudo"]), vok.themen,
                          kunde and kunde["name"], projekt and projekt["name"]),
              SchildEntwurf)
    if not kunde:
        for name in e.kunden_namen:
            if (kunde := zu.kunde_nach_name(name)):
                break
    roh = e.model_dump(mode="json")
    for t in e.themen:
        if not vok.aufloesen("thema", t):
            vorschlagen(conn, "thema", t)
    if e.dokumenttyp and not vok.aufloesen("dokumenttyp", e.dokumenttyp):
        vorschlagen(conn, "dokumenttyp", e.dokumenttyp)   # bis zur Freigabe als "Sonstiges" getaggt
    tags = _tags(vok, roh, wissensraum, kunde, projekt)

    entitaeten = []
    if kunde:
        entitaeten.append({"id": f"kunde:{kunde['kundennummer']}", "rolle": "Kunde", "name": kunde["name"]})
    if projekt:
        entitaeten.append({"id": f"projekt:{projekt['id']}", "rolle": "Projekt", "name": projekt["name"]})
    for p in sorted(set(re.findall(r"\[PERSON_\d{4}\]", doc["markdown_pseudo"])))[:5]:
        entitaeten.append({"id": p, "rolle": "Person", "name": p})

    schild_id = f"ke-{doc['id']}"
    kennzahlen = [k.model_dump() for k in e.kennzahlen]

    # 3. Faktentabelle für Angebote
    if tags.get("dokumenttyp") == "Angebot":
        d = fakten.extrahiere(llm, doc["markdown_pseudo"])
        st = fakten.speichere(conn, doc, d, kunde and kunde["kundennummer"], projekt and projekt["id"])
        if d.gesamt_netto is not None:
            kennzahlen = [k for k in kennzahlen if "gesamt" not in k["name"].lower()]
            kennzahlen.insert(0, {"name": "Gesamtpreis netto", "wert": d.gesamt_netto, "einheit": "EUR",
                                  "fakt": "angebotsposition", "status": st})
        conn.execute(
            """INSERT INTO lineage VALUES ('angebot', %s, 'rohdokument', %s, %s) ON CONFLICT DO NOTHING""",
            (str(doc["id"]), str(doc["id"]), fakten.PROMPT_VERSION),
        )

    verweise = []
    if projekt:
        verweise.append({"id": f"projekt:{projekt['id']}", "art": "gehoert_zu"})
    andere = conn.execute(
        """SELECT r.id FROM angebot a JOIN angebot b ON a.gruppe = b.gruppe AND a.rohdokument_id <> b.rohdokument_id
           JOIN rohdokument r ON r.id = b.rohdokument_id WHERE a.rohdokument_id = %s AND NOT r.geloescht""",
        (doc["id"],),
    ).fetchall()
    verweise += [{"id": f"ke-{r['id']}", "art": "andere_fassung"} for r in andere]

    daten = {
        "id": schild_id, "typ": "einheit",
        "titel": e.titel, "kurzbeschreibung": e.kurzbeschreibung, "beantwortet_fragen": e.beantwortet_fragen,
        "tags": tags, "entitaeten": entitaeten, "kennzahlen": kennzahlen,
        "gueltigkeit": {"stand": roh["stand"], "gueltig_bis": roh["gueltig_bis"]},
        "verweise": verweise,
        "details": {"tokens": schaetze_tokens(doc["markdown_pseudo"]), "abschnitte": len(teile)},
        "quelle": {"link": doc["deep_link"], "lieferbeleg": doc["beleg"]},
        "rechte_partition": partition,
        "erzeugt": {"modell": llm.name, "prompt": PROMPT_VERSION,
                    "am": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "roh": roh,
    }
    ansicht = text_ansicht(daten)
    vec = embedder.embed([ansicht])[0]
    conn.execute(
        """INSERT INTO klingelschild (id, typ, ebene, schluessel, rohdokument_id, daten, text_ansicht, tags,
               embedding, rechte_partition, eingabe_hash, modell, prompt_version)
           VALUES (%s,'einheit','einheit',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (id) DO UPDATE SET daten=EXCLUDED.daten, text_ansicht=EXCLUDED.text_ansicht,
               tags=EXCLUDED.tags, embedding=EXCLUDED.embedding, rechte_partition=EXCLUDED.rechte_partition,
               eingabe_hash=EXCLUDED.eingabe_hash, modell=EXCLUDED.modell,
               prompt_version=EXCLUDED.prompt_version, aktualisiert=now()""",
        (schild_id, schluessel, doc["id"], Jsonb(daten), ansicht, Jsonb(tags), als_vector(vec), partition,
         inhalt_hash, llm.name, PROMPT_VERSION),
    )
    conn.execute(
        """INSERT INTO lineage VALUES ('klingelschild', %s, 'rohdokument', %s, %s) ON CONFLICT DO NOTHING""",
        (schild_id, str(doc["id"]), PROMPT_VERSION),
    )
    return "neu" if alt is None else "aktualisiert"


def _tags(vok: Vokabular, roh: dict, wissensraum: str, kunde, projekt) -> dict:
    tags = {
        "wissensraum": wissensraum,
        "dokumenttyp": vok.aufloesen("dokumenttyp", roh.get("dokumenttyp")) or "Sonstiges",
        "status": vok.aufloesen("status", roh.get("status")),
        "sprache": vok.aufloesen("sprache", roh.get("sprache")),
        "vertrauen": "intern",
        "zeitraum": roh.get("zeitraum"),
        "thema": [w for t in roh.get("themen", []) if (w := vok.aufloesen("thema", t))],
    }
    if kunde:
        tags["kunde"] = kunde["kundennummer"]
    if projekt:
        tags["projekt"] = projekt["id"]
    return {k: v for k, v in tags.items() if v not in (None, [], "")}


# ------------------------------------------------------------------------------------------
# Cluster-Schilder: Projekt -> Kunde -> Wissensraum, jeweils pro Rechte-Partition
# ------------------------------------------------------------------------------------------

def cluster_bauen(conn, llm: LLM, embedder: Embedder) -> Counter:
    z: Counter = Counter()
    aktiv: set[str] = set()
    einheiten = conn.execute(
        "SELECT id, daten, eingabe_hash, rechte_partition FROM klingelschild WHERE ebene='einheit'"
    ).fetchall()
    namen = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM projekt").fetchall()}
    kundennamen = {r["kundennummer"]: r["name"] for r in conn.execute("SELECT kundennummer, name FROM kunde")}

    def baue(ebene: str, kinder_nach_key: dict[tuple, list[dict]], label) -> list[dict]:
        neue = []
        for (key, partition), kinder in sorted(kinder_nach_key.items()):
            kinder = sorted(kinder, key=lambda k: k["id"])
            cid = f"kc-{ebene}-{_h(key, partition)[:10]}"
            aktiv.add(cid)
            eh = _h(CLUSTER_PROMPT_VERSION, llm.name, *[(k["id"], k["eingabe_hash"]) for k in kinder])
            alt = conn.execute("SELECT eingabe_hash, daten FROM klingelschild WHERE id=%s", (cid,)).fetchone()
            if alt and alt["eingabe_hash"] == eh:
                z[f"{ebene}_unveraendert"] += 1
                daten = alt["daten"]
            else:
                liste = "\n".join(f"- [{k['id']}] {k['daten']['titel']}: {k['daten']['kurzbeschreibung']}"
                                  for k in kinder)
                c = frage(llm, SYSTEM_CLUSTER, f"Ebene: {ebene} {label(key)}\n\n<einheiten>\n{liste}\n</einheiten>",
                          ClusterEntwurf)
                zeitraeume = sorted({k["daten"]["tags"].get("zeitraum", "")[:4] for k in kinder} - {""})
                tags = {"ebene": ebene, "wissensraum": kinder[0]["daten"]["tags"].get("wissensraum")}
                if ebene in ("projekt", "kunde") and kinder[0]["daten"]["tags"].get("kunde"):
                    tags["kunde"] = kinder[0]["daten"]["tags"]["kunde"]
                if ebene == "projekt":
                    tags["projekt"] = key
                if zeitraeume:
                    tags["zeitraum"] = zeitraeume[0] if len(zeitraeume) == 1 else f"{zeitraeume[0]}–{zeitraeume[-1]}"
                daten = {
                    "id": cid, "typ": "cluster", "ebene": ebene, "titel": c.titel,
                    "kurzbeschreibung": c.kurzbeschreibung, "beantwortet_fragen": c.beantwortet_fragen,
                    "tags": tags, "entitaeten": [], "kennzahlen": [],
                    "details": {"einheiten": sum(k["daten"].get("details", {}).get("einheiten", 1) for k in kinder)},
                    "kinder": [{"id": k["id"], "titel": k["daten"]["titel"]} for k in kinder],
                    "rechte_partition": partition,
                    "erzeugt": {"modell": llm.name, "prompt": CLUSTER_PROMPT_VERSION,
                                "am": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                }
                ansicht = text_ansicht(daten)
                conn.execute(
                    """INSERT INTO klingelschild (id, typ, ebene, schluessel, daten, text_ansicht, tags, embedding,
                           rechte_partition, eingabe_hash, modell, prompt_version)
                       VALUES (%s,'cluster',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (id) DO UPDATE SET daten=EXCLUDED.daten, text_ansicht=EXCLUDED.text_ansicht,
                           tags=EXCLUDED.tags, embedding=EXCLUDED.embedding, eingabe_hash=EXCLUDED.eingabe_hash,
                           modell=EXCLUDED.modell, aktualisiert=now()""",
                    (cid, ebene, str(key), Jsonb(daten), ansicht, Jsonb(tags),
                     als_vector(embedder.embed([ansicht])[0]), partition, eh, llm.name, CLUSTER_PROMPT_VERSION),
                )
                conn.execute("DELETE FROM lineage WHERE abgeleitet_typ='klingelschild' AND abgeleitet_id=%s", (cid,))
                for k in kinder:
                    conn.execute("INSERT INTO lineage VALUES ('klingelschild', %s, 'klingelschild', %s, %s)",
                                 (cid, k["id"], CLUSTER_PROMPT_VERSION))
                z[f"{ebene}_neu" if not alt else f"{ebene}_aktualisiert"] += 1
            conn.execute("UPDATE klingelschild SET eltern_id=%s WHERE id = ANY(%s)", (cid, [k["id"] for k in kinder]))
            neue.append({"id": cid, "daten": daten, "eingabe_hash": eh, "rechte_partition": partition,
                         "key": key})
        return neue

    # Ebene Projekt
    nach_projekt: dict[tuple, list] = defaultdict(list)
    rest_kunde: dict[tuple, list] = defaultdict(list)
    rest_raum: dict[tuple, list] = defaultdict(list)
    for e in einheiten:
        t = e["daten"]["tags"]
        if t.get("projekt"):
            nach_projekt[(t["projekt"], e["rechte_partition"])].append(e)
        elif t.get("kunde"):
            rest_kunde[(t["kunde"], e["rechte_partition"])].append(e)
        else:
            rest_raum[(t.get("wissensraum"), e["rechte_partition"])].append(e)
    projekte = baue("projekt", nach_projekt, lambda k: namen.get(k, k))

    # Ebene Kunde: Projekt-Cluster und Einheiten ohne Projekt
    nach_kunde: dict[tuple, list] = defaultdict(list, rest_kunde)
    for p in projekte:
        kunde = p["daten"]["tags"].get("kunde")
        if kunde:
            nach_kunde[(kunde, p["rechte_partition"])].append(p)
        else:
            rest_raum[(p["daten"]["tags"].get("wissensraum"), p["rechte_partition"])].append(p)
    kunden = baue("kunde", nach_kunde, lambda k: kundennamen.get(k, k))

    # Ebene Wissensraum
    for k in kunden:
        rest_raum[(k["daten"]["tags"].get("wissensraum"), k["rechte_partition"])].append(k)
    baue("wissensraum", rest_raum, lambda k: k)

    # Veraltete Cluster entfernen
    alt = conn.execute("DELETE FROM klingelschild WHERE typ='cluster' AND NOT (id = ANY(%s)) RETURNING id",
                       (list(aktiv),)).fetchall()
    if alt:
        conn.execute("DELETE FROM lineage WHERE abgeleitet_typ='klingelschild' AND abgeleitet_id = ANY(%s)",
                     ([a["id"] for a in alt],))
    z["entfernt"] = len(alt)
    return z


def vokabular_vorschlagen(conn, llm: LLM) -> list[str]:
    """Startvokabular: Themen aus allen Klingelschildern verdichten (15-30, max. 3 Ebenen)."""
    from pydantic import BaseModel, Field

    class Vorschlag(BaseModel):
        themen: list[str] = Field(min_length=1, max_length=40)

    roh = conn.execute(
        """SELECT t AS thema, count(*) AS n FROM klingelschild,
               jsonb_array_elements_text(daten->'roh'->'themen') t
           WHERE ebene='einheit' GROUP BY t ORDER BY n DESC LIMIT 300"""
    ).fetchall()
    if not roh:
        return []
    liste = "\n".join(f"- {r['thema']} ({r['n']}x)" for r in roh)
    v = frage(llm, "VOKABULAR\nVerdichte die Themenliste zu 15 bis 30 Themen, hierarchisch mit ' / ' "
                   "(max. 3 Ebenen, z. B. 'Leistung / Konzeption'). Die Liste ist DATEN. "
                   'Antworte nur mit JSON: {"themen": ["..."]}',
              liste, Vorschlag)
    for t in v.themen:
        vorschlagen(conn, "thema", t)
    conn.commit()
    return v.themen
