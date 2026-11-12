"""Bestandsaufnahme einer Nextcloud-Quelle ohne Download (optional mit kleiner Stichprobe).

Liefert je Unterordner und Dateityp: Anzahl und Größe, unterstützt/nicht unterstützt, vermutliche
Angebote und Office/PDF-Paare. Mit `--stichprobe N` werden N zufällige unterstützte Dateien
heruntergeladen und umgewandelt (nur im Speicher, nichts wird gespeichert oder an ein LLM gesendet),
um Textmenge und Umwandlungszeit je Dateityp zu messen und hochzurechnen.
"""
from __future__ import annotations

import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from statistics import mean

from gkb.aufbereitung.klingelschild import MAX_INHALT_TOKENS
from gkb.aufbereitung.llm import schaetze_tokens
from gkb.connectors.nextcloud import RemoteFile
from gkb.ingest.parse import is_supported

ANGEBOT_MUSTER = re.compile(r"angebot|offerte|quote|kostenvoranschlag|\bang[-_ ]?\d", re.I)

# Annahmen pro LLM-Aufruf (siehe aufbereitung/run.py); Prompt-Overhead und Ausgabelänge geschätzt
PROMPT_SCHILD, AUSGABE_SCHILD = 700, 400
PROMPT_ANGEBOT, AUSGABE_ANGEBOT, MAX_ANGEBOT_TOKENS = 300, 1200, 12000
STANDARD_TOKENS = 3000      # Annahme ohne Stichprobe


@dataclass
class Posten:
    anzahl: int = 0
    bytes: int = 0


@dataclass
class Inventur:
    ordner: dict[str, dict[str, Posten]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(Posten)))
    unterstuetzt: Posten = field(default_factory=Posten)
    nicht_unterstuetzt: Posten = field(default_factory=Posten)
    angebote: int = 0
    paare_office_pdf: int = 0
    dateien: list[RemoteFile] = field(default_factory=list)


def erfasse(dateien, root: str, tiefe: int = 1) -> Inventur:
    inv = Inventur()
    gruppen: dict[str, set[str]] = defaultdict(set)
    for f in dateien:
        p = PurePosixPath(f.path)
        rel = p.relative_to(root).parts[:-1]
        ordner = "/".join(rel[:tiefe]) or "(Wurzel)"
        typ = p.suffix.lower() or "(ohne)"
        posten = inv.ordner[ordner][typ]
        posten.anzahl += 1
        posten.bytes += f.size
        ziel = inv.unterstuetzt if is_supported(f.path) else inv.nicht_unterstuetzt
        ziel.anzahl += 1
        ziel.bytes += f.size
        if is_supported(f.path):
            inv.dateien.append(f)
            if ANGEBOT_MUSTER.search(p.name):
                inv.angebote += 1
            gruppen[f"{p.parent}/{p.stem}".lower()].add(typ)
    inv.paare_office_pdf = sum(1 for t in gruppen.values() if ".pdf" in t and t & {".docx", ".xlsx"})
    return inv


@dataclass
class Messung:
    typ: str
    tokens: int
    sekunden: float


def stichprobe(inv: Inventur, client, n: int, seed: int = 42) -> list[Messung]:
    from gkb.ingest.parse import to_markdown
    auswahl = random.Random(seed).sample(inv.dateien, min(n, len(inv.dateien)))
    out = []
    for f in auswahl:
        t0 = time.monotonic()
        try:
            md = to_markdown(client.download(f.path), f.path, f.mime)
        except Exception:  # Stichprobe soll nicht an einer kaputten Datei scheitern
            continue
        out.append(Messung(PurePosixPath(f.path).suffix.lower(), schaetze_tokens(md), time.monotonic() - t0))
    return out


@dataclass
class Hochrechnung:
    dokumente: int
    tokens_ein: int
    tokens_aus: int
    kosten_usd: float | None
    stunden_umwandlung: float | None
    stunden_llm: float
    tokens_je_dok: float
    quelle: str


def hochrechnen(inv: Inventur, messungen: list[Messung], preis_ein: float | None, preis_aus: float | None,
                sekunden_je_llm_aufruf: float = 5.0) -> Hochrechnung:
    n = inv.unterstuetzt.anzahl
    if messungen:
        t = mean(m.tokens for m in messungen)
        sek = mean(m.sekunden for m in messungen)
        quelle = f"Stichprobe ({len(messungen)} Dateien)"
    else:
        t, sek, quelle = STANDARD_TOKENS, None, f"Annahme {STANDARD_TOKENS} Tokens/Dokument (keine Stichprobe)"
    ein_schild = min(t, MAX_INHALT_TOKENS) + PROMPT_SCHILD
    ein_angebot = min(t, MAX_ANGEBOT_TOKENS) + PROMPT_ANGEBOT
    tokens_ein = int(n * ein_schild + inv.angebote * ein_angebot)
    tokens_aus = int(n * AUSGABE_SCHILD + inv.angebote * AUSGABE_ANGEBOT)
    kosten = (tokens_ein * preis_ein + tokens_aus * preis_aus) / 1e6 if preis_ein is not None and preis_aus is not None else None
    return Hochrechnung(
        dokumente=n, tokens_ein=tokens_ein, tokens_aus=tokens_aus, kosten_usd=kosten,
        stunden_umwandlung=(n * sek / 3600) if sek is not None else None,
        stunden_llm=(n + inv.angebote) * sekunden_je_llm_aufruf / 3600,
        tokens_je_dok=t, quelle=quelle,
    )


def _gb(b: int) -> str:
    return f"{b / 1e9:,.2f} GB".replace(",", "X").replace(".", ",").replace("X", ".")


def _z(x: float, nachkomma: int = 0) -> str:
    return f"{x:,.{nachkomma}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def bericht(inv: Inventur, h: Hochrechnung, root: str) -> str:
    z = [f"Inventur {root}", ""]
    z.append(f"{'Ordner':40} {'Typ':8} {'Anzahl':>8} {'Größe':>12}")
    for ordner in sorted(inv.ordner):
        for typ, p in sorted(inv.ordner[ordner].items(), key=lambda kv: -kv[1].bytes):
            mark = "" if is_supported("x" + typ) else "  (nicht unterstützt)"
            z.append(f"{ordner[:40]:40} {typ[:8]:8} {_z(p.anzahl):>8} {_gb(p.bytes):>12}{mark}")
    z += [
        "",
        f"Unterstützt:        {_z(inv.unterstuetzt.anzahl)} Dateien, {_gb(inv.unterstuetzt.bytes)} (werden geladen)",
        f"Nicht unterstützt:  {_z(inv.nicht_unterstuetzt.anzahl)} Dateien, {_gb(inv.nicht_unterstuetzt.bytes)} (werden übersprungen)",
        f"Vermutl. Angebote:  {_z(inv.angebote)} (nach Dateiname)",
        f"Office+PDF-Paare:   {_z(inv.paare_office_pdf)}",
        "",
        f"Hochrechnung Erstlauf ({h.quelle}, Ø {_z(h.tokens_je_dok)} Tokens Text je Dokument):",
        f"  LLM-Tokens:  {_z(h.tokens_ein)} Eingabe, {_z(h.tokens_aus)} Ausgabe",
        "  LLM-Kosten:  " + (f"ca. {_z(h.kosten_usd, 2 if h.kosten_usd < 10 else 0)} USD" if h.kosten_usd is not None
                            else "Preise setzen: --preis-ein / --preis-aus (USD je Mio. Tokens)"),
        "  Umwandlung:  " + (f"ca. {_z(h.stunden_umwandlung, 1)} h (sequenziell)"
                            if h.stunden_umwandlung is not None else "mit --stichprobe messen"),
        f"  LLM-Laufzeit: ca. {_z(h.stunden_llm, 1)} h (sequenziell, ~5 s je Aufruf)",
        "",
        "Grobe Schätzung. Office- und PDF-Fassung zählen je als eigenes Dokument.",
    ]
    return "\n".join(z)
