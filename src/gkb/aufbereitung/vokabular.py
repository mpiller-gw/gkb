"""Kontrolliertes Vokabular für die Tags (Stufe 1).

Feste Facetten sind vorbelegt. Das Themenvokabular wächst aus dem Bestand:
KI-Vorschläge landen mit Status 'vorschlag' und werden erst nach Freigabe als Tag vergeben.
"""
from __future__ import annotations

import re

FEST = {
    "dokumenttyp": {
        "Angebot": ["Offerte", "Kostenvoranschlag", "Quote"],
        "Projektbeschreibung": ["Projektsteckbrief", "Projektskizze"],
        "Konzept": ["Grobkonzept", "Feinkonzept"],
        "Protokoll": ["Besprechungsprotokoll", "Meeting-Notiz"],
        "Vertrag": ["Rahmenvertrag", "Auftrag"],
        "Präsentation": ["Pitch", "Folien"],
        "Sonstiges": [],
    },
    "status": {w: [] for w in ["Entwurf", "versendet", "angenommen", "abgelehnt", "abgeschlossen"]},
    "sprache": {"de": [], "en": []},
    "vertrauen": {w: [] for w in ["verifiziert", "intern", "Lieferant", "extern"]},
}


def norm(w: str) -> str:
    return re.sub(r"\s+", " ", w).strip().lower()


def vorbelegen(conn) -> None:
    for facette, werte in FEST.items():
        for wert, syn in werte.items():
            conn.execute(
                """INSERT INTO vokabular (facette, wert, synonyme) VALUES (%s, %s, %s)
                   ON CONFLICT (facette, wert) DO NOTHING""",
                (facette, wert, syn),
            )
    conn.commit()


class Vokabular:
    def __init__(self, conn):
        self._map: dict[tuple[str, str], str] = {}
        rows = conn.execute("SELECT facette, wert, synonyme FROM vokabular WHERE status = 'aktiv'").fetchall()
        for r in rows:
            self._map[(r["facette"], norm(r["wert"]))] = r["wert"]
            for s in r["synonyme"]:
                self._map[(r["facette"], norm(s))] = r["wert"]
        self.themen = sorted(r["wert"] for r in rows if r["facette"] == "thema")
        # Hierarchische Themen auch über ihr letztes Segment finden ("Konzeption" -> "Leistung / Konzeption")
        letzte: dict[str, list[str]] = {}
        for t in self.themen:
            letzte.setdefault(norm(t.split("/")[-1]), []).append(t)
        for seg, werte in letzte.items():
            if len(werte) == 1:
                self._map.setdefault(("thema", seg), werte[0])

    def aufloesen(self, facette: str, wert: str | None) -> str | None:
        if not wert:
            return None
        return self._map.get((facette, norm(wert)))


def vorschlagen(conn, facette: str, wert: str) -> None:
    conn.execute(
        """INSERT INTO vokabular (facette, wert, status, anzahl) VALUES (%s, %s, 'vorschlag', 1)
           ON CONFLICT (facette, wert) DO UPDATE SET anzahl = vokabular.anzahl + 1""",
        (facette, wert.strip()),
    )


def freigeben(conn, werte: list[str] | None, facette: str = "thema") -> int:
    if werte:
        n = conn.execute(
            "UPDATE vokabular SET status='aktiv' WHERE facette=%s AND status='vorschlag' AND wert = ANY(%s)",
            (facette, werte),
        ).rowcount
    else:
        n = conn.execute(
            "UPDATE vokabular SET status='aktiv' WHERE facette=%s AND status='vorschlag'", (facette,)
        ).rowcount
    conn.commit()
    return n
