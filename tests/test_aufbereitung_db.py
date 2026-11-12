"""Integrationstest Baustein 2 gegen Postgres, mit Platzhalter-LLM und Hash-Embeddings."""
import os

import pytest
from cryptography.fernet import Fernet

from gkb.aufbereitung import vokabular
from gkb.aufbereitung.abschnitte import zerlege
from gkb.aufbereitung.embeddings import HashEmbedder
from gkb.aufbereitung.fakten import AngebotDaten, Position, pruefe
from gkb.aufbereitung.klingelschild import text_ansicht
from gkb.aufbereitung.llm import schaetze_tokens
from gkb.aufbereitung.platzhalter import PlatzhalterLLM
from gkb.aufbereitung.run import aufbereiten
from gkb.db import connect, init_db, quelle_id
from gkb.ingest.pseudonymize import PgVault, Pseudonymizer

URL = os.environ.get("GKB_TEST_DATABASE_URL")

ANGEBOT = """# Angebot Website-Relaunch

Angebotsnummer: A-2025-031
Ansprechpartner [PERSON_0001], Stand 2025.

| Pos | Leistung | Menge | Einheit | Einzelpreis | Gesamt |
|---|---|---|---|---|---|
| 1 | Konzeption | 10 | Tag | 1.200,00 | 12.000,00 |
| 2 | Umsetzung | 30 | Tag | 1.100,00 | 33.000,00 |
| 3 | Content-Import | 1 | pauschal | 3.500,00 | 3.500,00 |

Gesamtpreis netto: 48.500,00
"""


class ZaehlLLM(PlatzhalterLLM):
    def __init__(self):
        self.aufrufe = 0

    def json(self, system, user):
        self.aufrufe += 1
        return super().json(system, user)


def test_zerlegung_haelt_ueberschriften():
    teile = zerlege("# A\n\nText eins\n\n## B\n\n" + ("Satz. " * 600))
    assert teile[0].ueberschrift == "A" and all(t.tokens <= 500 for t in teile)
    assert {t.ueberschrift for t in teile} == {"A", "B"}


def test_plausibilitaet():
    ok = AngebotDaten(gesamt_netto=100, positionen=[Position(leistung="a", gesamt=60), Position(leistung="b", gesamt=40)])
    assert pruefe(ok)[0] == "ok"
    falsch = AngebotDaten(gesamt_netto=120, positionen=[Position(leistung="a", gesamt=60)])
    assert pruefe(falsch)[0] == "pruefen"
    assert pruefe(AngebotDaten())[0] == "pruefen"


def test_textansicht_kompakt():
    d = {"id": "ke-1", "titel": "Angebot Relaunch Beispiel GmbH", "kurzbeschreibung": "Drittes Angebot.",
         "beantwortet_fragen": ["Was kostet es?", "Was ist enthalten?", "Was ist neu?"],
         "tags": {"dokumenttyp": "Angebot", "zeitraum": "2025-Q3", "vertrauen": "intern"},
         "entitaeten": [{"rolle": "Kunde", "name": "Beispiel GmbH"}],
         "kennzahlen": [{"name": "Gesamtpreis netto", "wert": 48500.0, "einheit": "EUR"}],
         "details": {"tokens": 4200}, "rechte_partition": "rp-geheim", "erzeugt": {"modell": "x"}}
    t = text_ansicht(d)
    assert "48.500 EUR" in t and "Details 4.200 Tokens" in t
    assert "rp-geheim" not in t and "modell" not in t       # Verwaltungsfelder sieht das LLM nie
    assert 30 < schaetze_tokens(t) < 150


@pytest.mark.skipif(not URL, reason="GKB_TEST_DATABASE_URL nicht gesetzt")
def test_aufbereitung_ablauf():
    conn = connect(URL)
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    init_db(conn)
    vault = PgVault(conn, Fernet.generate_key().decode(), "hmac")
    pseudo = Pseudonymizer(vault)
    qid = quelle_id(conn, "nextcloud", "test")
    conn.execute("INSERT INTO kunde (kundennummer, name) VALUES ('10001', 'Beispiel GmbH')")
    for rp in ("rp-a", "rp-b"):
        conn.execute("INSERT INTO rechte_partition (id, erlaubt) VALUES (%s, '{group:vertrieb}')", (rp,))

    def doc(ext_id, pfad, md, rp="rp-a"):
        return conn.execute(
            """INSERT INTO rohdokument (quelle_id, externe_id, titel, pfad, deep_link, sha256, markdown_pseudo,
                   rechte_partition) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (qid, ext_id, pfad.rsplit("/", 1)[1].rsplit(".", 1)[0], pfad, f"https://c/f/{ext_id}",
             f"sha-{ext_id}-{len(md)}", md, rp),
        ).fetchone()["id"]

    d_pdf = doc("1", "Projekte/Beispiel GmbH/Relaunch 2025/Angebot v3.pdf", ANGEBOT)
    d_docx = doc("2", "Projekte/Beispiel GmbH/Relaunch 2025/Angebot v3.docx", ANGEBOT.replace("48.500,00\n", "49.500,00\n"))
    doc("3", "Projekte/Beispiel GmbH/Relaunch 2025/Konzept.md", "# Konzept\n\nKonzept für den Relaunch 2025.")
    doc("4", "Projekte/Allgemein.md", "# Leitfaden\n\nInterne Hinweise.")
    conn.commit()

    llm, emb = ZaehlLLM(), HashEmbedder()
    z = aufbereiten(conn, llm, emb, pseudo)
    assert z["neu"] == 4 and z["fehler"] == 0

    # Faktentabelle: PDF plausibel, DOCX nicht; Fassungen weichen ab -> beide prüfen
    a = {r["rohdokument_id"]: r for r in conn.execute("SELECT * FROM angebot").fetchall()}
    assert a[d_pdf]["fassung"] == "pdf" and float(a[d_pdf]["gesamt_netto"]) == 48500.0
    assert a[d_pdf]["status"] == "pruefen" and "weichen ab" in a[d_pdf]["pruefhinweis"]
    assert "Summe Positionen" in a[d_docx]["pruefhinweis"]
    assert conn.execute("SELECT count(*) AS n FROM angebotsposition WHERE rohdokument_id=%s",
                        (d_pdf,)).fetchone()["n"] == 3
    m = conn.execute("SELECT * FROM angebot_massgeblich").fetchall()
    assert len(m) == 1 and m[0]["fassung"] == "pdf"

    # Klingelschild: Tags, Kunde aus dem Ordner, Kennzahl, Verweise
    s = conn.execute("SELECT * FROM klingelschild WHERE id=%s", (f"ke-{d_pdf}",)).fetchone()
    assert s["tags"]["kunde"] == "10001" and s["tags"]["dokumenttyp"] == "Angebot"
    assert s["daten"]["kennzahlen"][0]["wert"] == 48500.0
    assert "thema" not in s["tags"]                          # Themen erst nach Freigabe
    assert any(e["rolle"] == "Person" and e["id"] == "[PERSON_0001]" for e in s["daten"]["entitaeten"])
    assert "Projekte/" not in s["text_ansicht"]               # kein Klartextpfad in der LLM-Ansicht

    # Cluster: Projekt -> Kunde -> Wissensraum
    ebenen = {r["ebene"]: r for r in conn.execute("SELECT * FROM klingelschild WHERE typ='cluster'")}
    assert set(ebenen) == {"projekt", "kunde", "wissensraum"}
    assert len(ebenen["projekt"]["daten"]["kinder"]) == 3
    assert s["eltern_id"] == ebenen["projekt"]["id"]

    # Zweiter Lauf: nichts geändert -> keine LLM-Aufrufe
    llm.aufrufe = 0
    z = aufbereiten(conn, llm, emb, pseudo)
    assert z["unveraendert"] == 4 and llm.aufrufe == 0

    # Themen freigeben -> Tags ohne LLM
    assert conn.execute("SELECT count(*) AS n FROM vokabular WHERE status='vorschlag'").fetchone()["n"] >= 1
    vokabular.freigeben(conn, None)
    z = aufbereiten(conn, llm, emb, pseudo)
    assert llm.aufrufe == 0
    s = conn.execute("SELECT tags FROM klingelschild WHERE id=%s", (f"ke-{d_pdf}",)).fetchone()
    assert s["tags"]["thema"]

    # Rechte ändern sich -> umhängen ohne LLM; Cluster entstehen pro Partition neu
    conn.execute("UPDATE rohdokument SET rechte_partition='rp-b' WHERE id=%s", (d_pdf,))
    conn.commit()
    z = aufbereiten(conn, llm, emb, pseudo)
    assert z["rechte_aktualisiert"] == 1
    assert conn.execute("SELECT rechte_partition FROM angebotsposition WHERE rohdokument_id=%s LIMIT 1",
                        (d_pdf,)).fetchone()["rechte_partition"] == "rp-b"
    assert conn.execute("SELECT count(*) AS n FROM klingelschild WHERE ebene='projekt'").fetchone()["n"] == 2

    # Löschung: Schild, Abschnitte und Fakten verschwinden
    conn.execute("UPDATE rohdokument SET geloescht=true WHERE id=%s", (d_docx,))
    conn.commit()
    z = aufbereiten(conn, llm, emb, pseudo)
    assert z["geloescht"] == 1
    for t in ("abschnitt", "angebot", "angebotsposition"):
        assert conn.execute(f"SELECT count(*) AS n FROM {t} WHERE rohdokument_id=%s", (d_docx,)).fetchone()["n"] == 0
    conn.close()
