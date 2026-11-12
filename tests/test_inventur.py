from datetime import datetime, timezone

from gkb.connectors.nextcloud import RemoteFile
from gkb.inventur import Messung, bericht, erfasse, hochrechnen


def rf(path, size):
    return RemoteFile(path, path, "e", "", size, datetime.now(timezone.utc), False)


DATEIEN = [
    rf("Sales/Kunde A/Angebot 2025-031.pdf", 2_000_000),
    rf("Sales/Kunde A/Angebot 2025-031.docx", 300_000),
    rf("Sales/Kunde A/Moodboard.png", 8_000_000),
    rf("Sales/Kunde B/Konzept.pptx", 12_000_000),
    rf("Sales/Kunde B/Film.mp4", 900_000_000),
    rf("Sales/Notiz.txt", 1_000),
]


def test_erfassung_und_hochrechnung():
    inv = erfasse(DATEIEN, "Sales")
    assert inv.unterstuetzt.anzahl == 4 and inv.nicht_unterstuetzt.anzahl == 2
    assert inv.angebote == 2 and inv.paare_office_pdf == 1
    assert inv.ordner["Kunde A"][".png"].anzahl == 1 and "(Wurzel)" in inv.ordner

    ohne = hochrechnen(inv, [], 1.0, 5.0)
    assert ohne.stunden_umwandlung is None and ohne.kosten_usd > 0
    # 4 Schilder à (3000+700) + 2 Angebote à (3000+300) Eingabe
    assert ohne.tokens_ein == 4 * 3700 + 2 * 3300
    assert ohne.tokens_aus == 4 * 400 + 2 * 1200

    mit = hochrechnen(inv, [Messung(".pdf", 20000, 30.0), Messung(".docx", 1000, 2.0)], None, None)
    assert mit.kosten_usd is None and mit.stunden_umwandlung == 4 * 16.0 / 3600
    assert mit.tokens_ein == 4 * (6000 + 700) + 2 * (10500 + 300)   # Kappung bei 6.000 Tokens für das Schild

    text = bericht(inv, ohne, "Sales")
    assert "nicht unterstützt" in text and "Vermutl. Angebote:  2" in text
