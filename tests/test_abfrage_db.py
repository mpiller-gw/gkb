"""Baustein 3: Abfrage-Werkzeuge und MCP-Server gegen Postgres (Platzhalter-LLM, Hash-Embeddings)."""
import asyncio
import json
import os

import pytest
from cryptography.fernet import Fernet

from gkb.abfrage import suche
from gkb.abfrage.server import baue_server
from gkb.abfrage.zugriff import Nutzer, sichtbare_partitionen
from gkb.aufbereitung.embeddings import HashEmbedder
from gkb.aufbereitung.platzhalter import PlatzhalterLLM
from gkb.aufbereitung.run import aufbereiten
from gkb.db import connect, init_db, quelle_id
from gkb.ingest.pseudonymize import PgVault, Pseudonymizer

URL = os.environ.get("GKB_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GKB_TEST_DATABASE_URL nicht gesetzt")

ANGEBOT = """# Angebot Relaunch

Angebotsnummer: A-2025-031

| Pos | Leistung | Menge | Einheit | Einzelpreis | Gesamt |
|---|---|---|---|---|---|
| 1 | Konzeption | 10 | Tag | 1.200,00 | 12.000,00 |
| 2 | Umsetzung | 30 | Tag | 1.100,00 | 33.000,00 |

Gesamtpreis netto: 45.000,00
"""


@pytest.fixture(scope="module")
def db():
    conn = connect(URL)
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    init_db(conn)
    qid = quelle_id(conn, "nextcloud", "test")
    conn.execute("INSERT INTO kunde (kundennummer, name) VALUES ('10001', 'Beispiel GmbH')")
    conn.execute("INSERT INTO rechte_partition (id, erlaubt) VALUES ('rp-vertrieb', '{group:vertrieb}')")
    conn.execute("INSERT INTO rechte_partition (id, erlaubt, verboten) VALUES ('rp-gf', '{group:gf,group:vertrieb}', '{user:bernd}')")
    conn.execute("INSERT INTO rechte_partition (id, erlaubt, vollstaendig) VALUES ('rp-unklar', '{}', false)")
    docs = [
        ("1", "Sales/Beispiel GmbH/Relaunch/Angebot.pdf", ANGEBOT, "rp-vertrieb"),
        ("2", "Sales/Beispiel GmbH/Relaunch/Konzept.md", "# Konzept\n\nKonzept für den Relaunch mit Redaktionssystem.", "rp-vertrieb"),
        ("3", "Sales/Intern/Strategie.md", "# Strategie\n\nVertraulicher Strategieplan 2026.", "rp-gf"),
        ("4", "DD/Vertrag.md", "# Vertrag\n\nRahmenvertrag mit Kündigungsfrist.", "rp-unklar"),
    ]
    for ext, pfad, md, rp in docs:
        conn.execute(
            """INSERT INTO rohdokument (quelle_id, externe_id, titel, pfad, deep_link, sha256, markdown_pseudo,
                   rechte_partition) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (qid, ext, pfad.rsplit("/", 1)[1].rsplit(".", 1)[0], pfad, f"https://c/f/{ext}", f"sha{ext}", md, rp))
    conn.commit()
    pseudo = Pseudonymizer(PgVault(conn, Fernet.generate_key().decode(), "h"))
    aufbereiten(conn, PlatzhalterLLM(), HashEmbedder(), pseudo)
    yield conn
    conn.close()


ANNA = Nutzer("anna", frozenset({"user:anna", "group:vertrieb"}))
BERND = Nutzer("bernd", frozenset({"user:bernd", "group:vertrieb"}))
ADMIN = Nutzer("michael", frozenset({"user:michael"}), admin=True)
FREMD = Nutzer("gast", frozenset({"user:gast"}))


def test_rechtefilter(db):
    assert set(sichtbare_partitionen(db, ANNA)) == {"rp-vertrieb", "rp-gf"}
    assert set(sichtbare_partitionen(db, BERND)) == {"rp-vertrieb"}          # ausdrücklich verboten
    assert "rp-unklar" in sichtbare_partitionen(db, ADMIN)                    # unvollständig: nur Admins
    assert sichtbare_partitionen(db, FREMD) == []


def test_suche_und_details(db):
    emb = HashEmbedder()
    p = sichtbare_partitionen(db, BERND)
    treffer = suche.schilder_suchen(db, emb, p, "Konzept Relaunch Redaktionssystem", ebene="einheit")
    assert treffer and "Konzept" in treffer[0]["schild"]
    assert all("Strategie" not in t["schild"] for t in treffer)              # rp-gf ist für Bernd gesperrt
    gefiltert = suche.schilder_suchen(db, emb, p, "Relaunch", {"dokumenttyp": "Angebot"}, "einheit")
    assert len(gefiltert) == 1 and gefiltert[0]["id"].startswith("ke-")

    d = suche.details_lesen(db, emb, p, gefiltert[0]["id"], frage="Umsetzung Tage")
    assert "Umsetzung" in d["inhalt"] and d["quelle"].startswith("https://c/f/")
    gesperrt = db.execute("SELECT id FROM klingelschild WHERE rechte_partition='rp-gf' AND ebene='einheit'").fetchone()
    assert suche.details_lesen(db, emb, p, gesperrt["id"]) is None

    nav = suche.verweisen_folgen(db, p, gefiltert[0]["id"])
    assert nav["eltern"] and "Enthält" in nav["eltern"]

    tags = suche.tags_uebersicht(db, p)
    assert tags["einheiten_gesamt"] == 2 and tags["kunde"] == {"10001": 2}


def test_datenpfad(db):
    p = sichtbare_partitionen(db, ANNA)
    a = suche.angebote_abfragen(db, p, kunde="10001", mit_positionen=True)
    assert a[0]["gesamt_netto"] == 45000.0 and a[0]["status"] == "ok" and len(a[0]["positionen"]) == 2
    assert suche.angebote_abfragen(db, sichtbare_partitionen(db, FREMD)) == []


def test_mcp_server(db):
    server = baue_server(lambda: connect(URL), HashEmbedder(), BERND)
    namen = {t.name for t in asyncio.run(server.list_tools())}
    assert namen == {"tags_uebersicht", "klingelschilder_suchen", "details_lesen", "verweisen_folgen",
                     "angebote_abfragen"}
    r = asyncio.run(server.call_tool("klingelschilder_suchen", {"frage": "Strategie", "anzahl": 5}))
    text = r.content[0].text if r.content else "[]"
    assert "Strategieplan" not in text
    r = asyncio.run(server.call_tool("angebote_abfragen", {"kunde": "10001"}))
    daten = json.loads(r.content[0].text)
    assert (daten[0] if isinstance(daten, list) else daten)["gesamt_netto"] == 45000.0
