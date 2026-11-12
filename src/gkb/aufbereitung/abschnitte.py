"""Markdown in Abschnitte zerlegen: entlang der Überschriften, lange Teile nach Absätzen gepackt."""
from __future__ import annotations

import re
from dataclasses import dataclass

from gkb.aufbereitung.llm import schaetze_tokens

MAX_TOKENS = 500


@dataclass
class Abschnitt:
    ueberschrift: str
    text: str

    @property
    def tokens(self) -> int:
        return schaetze_tokens(self.text)


def zerlege(markdown: str, max_tokens: int = MAX_TOKENS) -> list[Abschnitt]:
    teile: list[tuple[str, str]] = []
    ueberschrift, puffer = "", []
    for zeile in markdown.splitlines():
        if re.match(r"^#{1,6}\s", zeile):
            if "".join(puffer).strip():
                teile.append((ueberschrift, "\n".join(puffer).strip()))
            ueberschrift, puffer = zeile.lstrip("#").strip(), []
        else:
            puffer.append(zeile)
    if "".join(puffer).strip():
        teile.append((ueberschrift, "\n".join(puffer).strip()))

    ergebnis: list[Abschnitt] = []
    for ue, text in teile:
        if schaetze_tokens(text) <= max_tokens:
            ergebnis.append(Abschnitt(ue, text))
            continue
        # Absätze packen; Tabellen bleiben als Block zusammen (Absätze sind durch Leerzeilen getrennt)
        block: list[str] = []
        for absatz in _kleine_stuecke(re.split(r"\n\s*\n", text), max_tokens):
            if block and schaetze_tokens("\n\n".join(block + [absatz])) > max_tokens:
                ergebnis.append(Abschnitt(ue, "\n\n".join(block)))
                block = []
            block.append(absatz)
        if block:
            ergebnis.append(Abschnitt(ue, "\n\n".join(block)))
    return ergebnis


def _kleine_stuecke(absaetze: list[str], max_tokens: int) -> list[str]:
    """Zu lange Absätze weiter teilen: erst nach Zeilen, dann nach Sätzen, zuletzt hart nach Länge."""
    grenze = max_tokens * 4
    out: list[str] = []
    for a in absaetze:
        if len(a) <= grenze:
            out.append(a)
            continue
        for zeile in a.splitlines() if "\n" in a else [a]:
            if len(zeile) <= grenze:
                out.append(zeile)
                continue
            satz_block = ""
            for satz in re.split(r"(?<=[.!?])\s+", zeile):
                while len(satz) > grenze:
                    out.append(satz[:grenze])
                    satz = satz[grenze:]
                if len(satz_block) + len(satz) + 1 > grenze:
                    out.append(satz_block.strip())
                    satz_block = ""
                satz_block += " " + satz
            if satz_block.strip():
                out.append(satz_block.strip())
    return out
