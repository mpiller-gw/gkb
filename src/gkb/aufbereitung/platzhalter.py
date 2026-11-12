"""Platzhalter-LLM ohne Netz: liefert regelbasierte, schema-konforme Antworten.

Nur für Tests und Trockenläufe des Ablaufs. Die Qualität ist bewusst einfach.
"""
from __future__ import annotations

import re


def _zahl(s: str) -> float | None:
    s = s.strip().replace("€", "").replace("EUR", "").strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".") if "," in s else s
    try:
        return float(s)
    except ValueError:
        return None


class PlatzhalterLLM:
    name = "platzhalter"

    def json(self, system: str, user: str) -> dict:
        art = system.split("\n", 1)[0].strip()
        doc = user.split("<dokument>", 1)[-1].split("</dokument>", 1)[0]
        if art == "KLINGELSCHILD":
            return self._schild(user, doc)
        if art == "ANGEBOT":
            return self._angebot(doc)
        if art == "CLUSTER":
            n = user.count("\n- [")
            ebene = user.split("\n", 1)[0].replace("Ebene: ", "")
            return {"titel": f"Übersicht {ebene}"[:80], "kurzbeschreibung": f"Gruppe mit {n} Einheiten.",
                    "beantwortet_fragen": ["Was gehört dazu?", "Welche Dokumente gibt es?", "Wie ist der Stand?"]}
        if art == "VOKABULAR":
            themen = re.findall(r"^- (.+?) \(\d+x\)$", user, re.M)
            return {"themen": themen[:30] or ["Allgemein"]}
        raise ValueError(f"unbekannte Anfrage: {art}")

    def _schild(self, user: str, doc: str) -> dict:
        datei = re.search(r"Dateiname: (.+)", user).group(1).strip()
        typ = "Angebot" if re.search(r"angebot", datei + doc, re.I) else (
            "Protokoll" if re.search(r"protokoll", datei + doc, re.I) else (
                "Reisekostenabrechnung" if re.search(r"abrechnung", datei + doc, re.I) else "Projektbeschreibung"))
        jahr = re.search(r"\b(20\d\d)\b", doc)
        erster_satz = re.sub(r"\s+", " ", re.sub(r"[#|*\-]", " ", doc)).strip()[:200]
        kurz = " ".join(erster_satz.split()[:40]) or "Kein Inhalt."
        gesamt = re.search(r"Gesamt(?:preis)?\s*netto[:\s|]*([\d.,]+)", doc, re.I)
        return {
            "titel": " ".join(datei.replace("_", " ").split()[:12]),
            "kurzbeschreibung": kurz,
            "beantwortet_fragen": [f"Was steht in {datei}?", "Worum geht es?", "Welche Eckdaten gibt es?"],
            "dokumenttyp": typ,
            "themen": ["Konzeption"] if "konzept" in doc.lower() else ["Umsetzung"],
            "zeitraum": jahr.group(1) if jahr else None,
            "status": None, "sprache": "de",
            "kunden_namen": [],
            "kennzahlen": [{"name": "Gesamtpreis netto", "wert": _zahl(gesamt.group(1)), "einheit": "EUR"}]
            if gesamt and _zahl(gesamt.group(1)) is not None else [],
            "stand": None, "gueltig_bis": None,
        }

    def _angebot(self, doc: str) -> dict:
        positionen = []
        for zeile in doc.splitlines():
            zellen = [z.strip() for z in zeile.strip().strip("|").split("|")]
            if len(zellen) == 6 and re.fullmatch(r"\d+(\.\d+)?", zellen[0]):
                positionen.append({"position": zellen[0], "leistung": zellen[1], "menge": _zahl(zellen[2]),
                                   "einheit": zellen[3], "einzelpreis": _zahl(zellen[4]), "gesamt": _zahl(zellen[5])})
        gesamt = re.search(r"Gesamt(?:preis)?\s*netto[:\s|]*([\d.,]+)", doc, re.I)
        nr = re.search(r"Angebot(?:snummer| Nr\.?)\s*:?\s*([\w-]+)", doc)
        return {"angebotsnummer": nr.group(1) if nr else None, "version": None,
                "gesamt_netto": _zahl(gesamt.group(1)) if gesamt else None, "gueltig_bis": None,
                "positionen": positionen}
