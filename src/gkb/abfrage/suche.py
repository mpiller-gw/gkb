"""Abfrage-Werkzeuge für den Agentenpfad (Stufe 1 -> 2 -> 3) und den Datenpfad.

Alle Funktionen bekommen die sichtbaren Partitionen und filtern damit in SQL.
Ergebnisse sind pseudonymisiert; Klarnamen verlassen den Server nicht.
"""
from __future__ import annotations

from gkb.aufbereitung.embeddings import Embedder, als_vector
from gkb.aufbereitung.llm import schaetze_tokens

FACETTEN = ("wissensraum", "dokumenttyp", "thema", "kunde", "projekt", "zeitraum", "status")
RRF_K = 60


def tags_uebersicht(conn, partitionen: list[str]) -> dict:
    """Stufe 1: Welche Tag-Werte gibt es (mit Anzahl Einheiten)?"""
    out: dict[str, dict[str, int]] = {}
    for f in FACETTEN:
        if f == "thema":
            sql = """SELECT t AS wert, count(*) AS n FROM klingelschild, jsonb_array_elements_text(tags->'thema') t
                     WHERE ebene='einheit' AND rechte_partition = ANY(%s) GROUP BY t ORDER BY n DESC LIMIT 50"""
        else:
            sql = f"""SELECT tags->>'{f}' AS wert, count(*) AS n FROM klingelschild
                      WHERE ebene='einheit' AND rechte_partition = ANY(%s) AND tags ? '{f}'
                      GROUP BY 1 ORDER BY n DESC LIMIT 50"""
        werte = {r["wert"]: r["n"] for r in conn.execute(sql, (partitionen,)).fetchall()}
        if werte:
            out[f] = werte
    out["einheiten_gesamt"] = conn.execute(
        "SELECT count(*) AS n FROM klingelschild WHERE ebene='einheit' AND rechte_partition = ANY(%s)",
        (partitionen,)).fetchone()["n"]
    return out


def _filter_sql(filter: dict | None) -> tuple[str, list]:
    teile, werte = [], []
    for k, v in (filter or {}).items():
        if k not in FACETTEN or v in (None, ""):
            continue
        if k == "thema":
            teile.append("tags->'thema' ? %s")
        elif k == "zeitraum":
            teile.append("tags->>'zeitraum' LIKE %s")
            v = f"{v}%"
        else:
            teile.append(f"tags->>'{k}' = %s")
        werte.append(str(v))
    return (" AND " + " AND ".join(teile)) if teile else "", werte


def schilder_suchen(conn, embedder: Embedder, partitionen: list[str], frage: str,
                    filter: dict | None = None, ebene: str | None = None, anzahl: int = 15) -> list[dict]:
    """Stufe 2: Klingelschilder hybrid suchen (Vektor + Volltext, Reciprocal Rank Fusion)."""
    anzahl = max(1, min(anzahl, 50))
    fsql, fwerte = _filter_sql(filter)
    esql = " AND ebene = %s" if ebene else ""
    ewerte = [ebene] if ebene else []
    basis = f"rechte_partition = ANY(%s){fsql}{esql}"
    vec = als_vector(embedder.embed([frage], art="query")[0])
    rows = conn.execute(
        f"""WITH v AS (
              SELECT id, row_number() OVER (ORDER BY embedding <=> %s::vector) AS r
              FROM klingelschild WHERE {basis} ORDER BY embedding <=> %s::vector LIMIT 100),
            t AS (
              SELECT id, row_number() OVER (ORDER BY ts_rank(to_tsvector('german', text_ansicht), q) DESC) AS r
              FROM klingelschild, websearch_to_tsquery('german', %s) q
              WHERE {basis} AND to_tsvector('german', text_ansicht) @@ q LIMIT 100)
            SELECT k.id, k.ebene, k.text_ansicht,
                   coalesce(1.0/({RRF_K}+v.r),0) + coalesce(1.0/({RRF_K}+t.r),0) AS score
            FROM klingelschild k LEFT JOIN v ON v.id=k.id LEFT JOIN t ON t.id=k.id
            WHERE v.id IS NOT NULL OR t.id IS NOT NULL
            ORDER BY score DESC LIMIT %s""",
        [vec, partitionen, *fwerte, *ewerte, vec, frage, partitionen, *fwerte, *ewerte, anzahl],
    ).fetchall()
    return [{"id": r["id"], "ebene": r["ebene"], "schild": r["text_ansicht"]} for r in rows]


def details_lesen(conn, embedder: Embedder, partitionen: list[str], schild_id: str,
                  frage: str | None = None, max_tokens: int = 4000) -> dict | None:
    """Stufe 3: Inhalt einer Einheit. Mit `frage` nur die passendsten Abschnitte bis zum Budget."""
    s = conn.execute(
        """SELECT k.rohdokument_id, k.text_ansicht, r.deep_link, r.titel FROM klingelschild k
           JOIN rohdokument r ON r.id = k.rohdokument_id
           WHERE k.id = %s AND k.ebene = 'einheit' AND k.rechte_partition = ANY(%s)""",
        (schild_id, partitionen),
    ).fetchone()
    if not s:
        return None
    if frage:
        rows = conn.execute(
            """SELECT nr, ueberschrift, text, tokens FROM abschnitt WHERE rohdokument_id=%s
               ORDER BY embedding <=> %s::vector""",
            (s["rohdokument_id"], als_vector(embedder.embed([frage], art="query")[0])),
        ).fetchall()
    else:
        rows = conn.execute("SELECT nr, ueberschrift, text, tokens FROM abschnitt WHERE rohdokument_id=%s ORDER BY nr",
                            (s["rohdokument_id"],)).fetchall()
    gewaehlt, budget = [], max(500, min(max_tokens, 12000))
    for r in rows:
        if gewaehlt and sum(x["tokens"] for x in gewaehlt) + r["tokens"] > budget:
            break
        gewaehlt.append(r)
    gewaehlt.sort(key=lambda r: r["nr"])
    text = "\n\n".join((f"## {r['ueberschrift']}\n\n" if r["ueberschrift"] else "") + r["text"] for r in gewaehlt)
    return {"id": schild_id, "titel": s["titel"], "quelle": s["deep_link"], "inhalt": text,
            "abschnitte": f"{len(gewaehlt)} von {len(rows)}", "tokens": schaetze_tokens(text)}


def verweisen_folgen(conn, partitionen: list[str], schild_id: str) -> dict | None:
    """Übergeordnetes Schild, Kinder und Verweise eines Schilds."""
    s = conn.execute("SELECT * FROM klingelschild WHERE id=%s AND rechte_partition = ANY(%s)",
                     (schild_id, partitionen)).fetchone()
    if not s:
        return None
    ids = [s["eltern_id"]] if s["eltern_id"] else []
    ids += [k["id"] for k in s["daten"].get("kinder", [])]
    ids += [v["id"] for v in s["daten"].get("verweise", []) if v["id"].startswith(("ke-", "kc-"))]
    rows = conn.execute("SELECT id, text_ansicht FROM klingelschild WHERE id = ANY(%s) AND rechte_partition = ANY(%s)",
                        (ids, partitionen)).fetchall()
    t = {r["id"]: r["text_ansicht"] for r in rows}
    return {"eltern": t.get(s["eltern_id"]),
            "kinder": [t[k["id"]] for k in s["daten"].get("kinder", []) if k["id"] in t],
            "verweise": [t[v["id"]] for v in s["daten"].get("verweise", []) if v["id"] in t]}


def angebote_abfragen(conn, partitionen: list[str], kunde: str | None = None, suchtext: str | None = None,
                      jahr: int | None = None, mit_positionen: bool = False, anzahl: int = 20) -> list[dict]:
    """Datenpfad: exakte Werte aus der Faktentabelle (maßgebliche Fassung je Angebot)."""
    sql = ["SELECT * FROM angebot_massgeblich WHERE rechte_partition = ANY(%s)"]
    werte: list = [partitionen]
    if kunde:
        sql.append("AND kunde = %s")
        werte.append(kunde)
    if suchtext:
        sql.append("AND (titel ILIKE %s OR angebotsnummer ILIKE %s)")
        werte += [f"%{suchtext}%", f"%{suchtext}%"]
    if jahr:
        sql.append("AND (extract(year FROM gueltig_bis) = %s OR titel LIKE %s)")
        werte += [jahr, f"%{jahr}%"]
    sql.append("ORDER BY gueltig_bis DESC NULLS LAST LIMIT %s")
    werte.append(max(1, min(anzahl, 100)))
    out = []
    for a in conn.execute(" ".join(sql), werte).fetchall():
        e = {k: (float(a[k]) if a[k] is not None and k in ("gesamt_netto", "summe_positionen") else a[k])
             for k in ("titel", "angebotsnummer", "version", "kunde", "projekt", "gesamt_netto",
                       "summe_positionen", "gueltig_bis", "status", "pruefhinweis", "deep_link")}
        e["gueltig_bis"] = str(e["gueltig_bis"]) if e["gueltig_bis"] else None
        e["schild"] = f"ke-{a['rohdokument_id']}"
        if mit_positionen:
            # Positionen bevorzugt aus der Office-Fassung derselben Gruppe
            quelle = conn.execute(
                """SELECT b.rohdokument_id FROM angebot b WHERE b.gruppe=%s AND b.rechte_partition = ANY(%s)
                   ORDER BY CASE b.fassung WHEN 'office' THEN 0 WHEN 'pdf' THEN 1 ELSE 2 END LIMIT 1""",
                (a["gruppe"], partitionen)).fetchone()
            e["positionen"] = [
                {k: (float(p[k]) if p[k] is not None and k in ("menge", "einzelpreis", "gesamt") else p[k])
                 for k in ("position", "leistung", "menge", "einheit", "einzelpreis", "gesamt")}
                for p in conn.execute("SELECT * FROM angebotsposition WHERE rohdokument_id=%s ORDER BY id",
                                      (quelle["rohdokument_id"],)).fetchall()
            ] if quelle else []
        out.append(e)
    return out
