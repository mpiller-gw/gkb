"""Integrationstest gegen eine echte Postgres-Datenbank (GKB_TEST_DATABASE_URL), Nextcloud wird simuliert."""
import os
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from gkb.connectors.nextcloud import RemoteFile
from gkb.db import connect, init_db, quelle_id
from gkb.ingest.pipeline import sync_nextcloud
from gkb.ingest.pseudonymize import PgVault, Pseudonymizer

URL = os.environ.get("GKB_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="GKB_TEST_DATABASE_URL nicht gesetzt")


class FakeNextcloud:
    def __init__(self):
        self.files: dict[str, tuple[RemoteFile, bytes]] = {}

    def put(self, path, fileid, etag, data):
        rf = RemoteFile(path, fileid, etag, "text/plain", len(data), datetime.now(timezone.utc), False)
        self.files[fileid] = (rf, data)

    def walk(self, root):
        return [rf for rf, _ in self.files.values() if rf.path.startswith(root)]

    def download(self, path):
        return next(d for rf, d in self.files.values() if rf.path == path)

    def deep_link(self, fileid):
        return f"https://cloud.test/f/{fileid}"


@pytest.fixture
def conn():
    c = connect(URL)
    c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    c.commit()
    init_db(c)
    yield c
    c.close()


def test_sync_ablauf(conn, tmp_path):
    vault = PgVault(conn, Fernet.generate_key().decode(), "hmac")
    pseudo = Pseudonymizer(vault, ["Erika Muster"])
    qid = quelle_id(conn, "nextcloud", "test")
    nc = FakeNextcloud()
    run = lambda: sync_nextcloud(conn, nc, pseudo, qid, "Projekte", str(tmp_path), 1)

    nc.put("Projekte/Kunde A/Angebot.txt", "1", "e1", "Ansprechpartnerin Erika Muster, 48.500 EUR".encode())
    nc.put("Projekte/Kunde A/Bild.png", "2", "e1", b"\x89PNG")
    z = run()
    assert z["neu"] == 1 and z["nicht_unterstuetzt"] == 1

    doc = conn.execute("SELECT * FROM rohdokument WHERE externe_id='1'").fetchone()
    assert "Erika" not in doc["markdown_pseudo"] and "[PERSON_0001]" in doc["markdown_pseudo"]
    assert doc["rechte_partition"] == "Projekte/Kunde A"
    assert doc["deep_link"] == "https://cloud.test/f/1"

    assert run()["unveraendert"] == 1                      # Stufe 1: nichts zu tun

    nc.put("Projekte/Kunde B/Angebot.txt", "1", "e2", nc.files["1"][1])   # verschoben, Inhalt gleich
    assert run()["verschoben"] == 1
    assert conn.execute("SELECT rechte_partition FROM rohdokument WHERE externe_id='1'").fetchone()[
        "rechte_partition"] == "Projekte/Kunde B"

    nc.put("Projekte/Kunde B/Kopie.txt", "3", "e1", nc.files["1"][1])    # Kopie unter neuer fileid
    assert run()["duplikat"] == 1
    assert conn.execute("SELECT count(*) AS n FROM dokument_herkunft").fetchone()["n"] == 1

    nc.put("Projekte/Kunde B/Angebot.txt", "1", "e3", b"Neuer Stand 49.000 EUR")
    assert run()["update"] == 1

    del nc.files["1"]
    assert run()["geloescht"] == 1
    assert conn.execute("SELECT geloescht FROM rohdokument WHERE externe_id='1'").fetchone()["geloescht"]

    lineage = conn.execute("SELECT count(*) AS n FROM lineage").fetchone()["n"]
    assert lineage == 2                                    # neu + update
