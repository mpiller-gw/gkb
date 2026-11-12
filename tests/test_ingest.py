from cryptography.fernet import Fernet
from openpyxl import Workbook

from gkb.connectors.nextcloud import parse_multistatus
from gkb.ingest.kunden import read_lexoffice
from gkb.ingest.pipeline import entscheide, rechte_partition
from gkb.ingest.pseudonymize import MemoryVault, Pseudonymizer
from gkb.storage import store_original

PREFIX = "/remote.php/dav/files/gkb-dienst/"

MULTISTATUS = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
 <d:response>
  <d:href>/remote.php/dav/files/gkb-dienst/Projekte/</d:href>
  <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype><oc:fileid>10</oc:fileid>
   <d:getetag>"aaa"</d:getetag></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
 <d:response>
  <d:href>/remote.php/dav/files/gkb-dienst/Projekte/Kunde%20A/Angebot%20v3.pdf</d:href>
  <d:propstat><d:prop><d:resourcetype/><oc:fileid>4711</oc:fileid><d:getetag>"e1"</d:getetag>
   <d:getcontenttype>application/pdf</d:getcontenttype><d:getcontentlength>1234</d:getcontentlength>
   <d:getlastmodified>Wed, 12 Nov 2025 10:00:00 GMT</d:getlastmodified></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
</d:multistatus>"""


def vault():
    return MemoryVault(Fernet.generate_key().decode(), "test-hmac")


def test_webdav_antwort_wird_gelesen():
    entries = parse_multistatus(MULTISTATUS, PREFIX)
    ordner, datei = entries
    assert ordner.is_dir and ordner.path == "Projekte"
    assert datei.path == "Projekte/Kunde A/Angebot v3.pdf"
    assert (datei.fileid, datei.etag, datei.size) == ("4711", "e1", 1234)
    assert datei.modified.year == 2025


def test_pseudonyme_sind_konsistent_und_umkehrbar():
    v = vault()
    p = Pseudonymizer(v, namen=["Max Müller"])
    text = "Max Müller (max.mueller@beispiel.de, +49 89 1234567) schrieb an max müller."
    out = p.pseudonymize(text)
    assert "Müller" not in out and "beispiel.de" not in out and "1234567" not in out
    assert out.count("[PERSON_0001]") == 2          # gleiche Person, gleiches Kürzel
    assert "[EMAIL_0001]" in out and "[TELEFON_0001]" in out
    assert v.reveal("[PERSON_0001]") == "Max Müller"


def test_iban_und_kurze_zahlen():
    p = Pseudonymizer(vault())
    out = p.pseudonymize("IBAN DE89 3704 0044 0532 0130 00, Angebot Nr. 2025-031, Preis 48.500 EUR")
    assert "[IBAN_0001]" in out
    assert "2025-031" in out and "48.500" in out    # keine Fehlerkennung als Telefon


def test_duplikat_entscheidung():
    assert entscheide(None, "h1", None).status == "neu"
    assert entscheide(None, "h1", 7).status == "duplikat"
    assert entscheide("h1", "h1", None).status == "verschoben"
    e = entscheide("h0", "h1", None)
    assert e.status == "update" and e.verarbeiten


def test_rechte_partition():
    assert rechte_partition("Projekte/Kunde A/2025/Angebot.pdf", "Projekte", 1) == "Projekte/Kunde A"
    assert rechte_partition("Projekte/Notiz.txt", "Projekte", 1) == "Projekte"


def test_original_ablage_nach_hash(tmp_path):
    h1, p1 = store_original(str(tmp_path), b"inhalt", ".pdf")
    h2, p2 = store_original(str(tmp_path), b"inhalt", ".pdf")
    assert h1 == h2 and p1 == p2 and p1.endswith(".pdf")


def test_lexoffice_import(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Kundenliste"])
    ws.append(["Kundennummer", "Firma", "Vorname", "Nachname", "E-Mail"])
    ws.append([10001, "Beispiel GmbH", "Erika", "Muster", "erika@beispiel.de"])
    ws.append([10002, None, "Hans", "Solo", "hans@gmail.com"])
    f = tmp_path / "kunden.xlsx"
    wb.save(f)
    a, b = read_lexoffice(f)
    assert (a.kundennummer, a.name, a.ist_person, a.domains) == ("10001", "Beispiel GmbH", False, ["beispiel.de"])
    assert a.personen == ["Erika Muster"]
    assert b.ist_person and b.name == "Hans Solo" and b.domains == []
