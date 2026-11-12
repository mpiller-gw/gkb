"""Prüft die Laufzeitumgebung: GPU, Docling, Embeddings, LLM-Verbindung. Sendet keine Dokumente."""
from __future__ import annotations

import time


def _zeile(ok: bool | None, text: str) -> None:
    mark = {True: "OK  ", False: "FEHL", None: "--  "}[ok]
    print(f"[{mark}] {text}")


def pruefen(s) -> bool:
    alles_ok = True

    try:
        import torch
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            frei, gesamt = torch.cuda.mem_get_info()
            _zeile(True, f"GPU: {p.name}, {gesamt / 1e9:.1f} GB, davon frei {frei / 1e9:.1f} GB")
        else:
            _zeile(None, "Keine GPU im Container – Docling und Embeddings laufen auf der CPU")
    except ImportError:
        _zeile(None, "PyTorch nicht installiert")

    try:
        import docling  # noqa: F401
        _zeile(True, "Docling installiert (nutzt die GPU automatisch, falls vorhanden)")
    except ImportError:
        _zeile(False, "Docling fehlt (Extra 'parse')")
        alles_ok = False

    try:
        from gkb.aufbereitung.embeddings import SentenceTransformerEmbedder
        t0 = time.monotonic()
        e = SentenceTransformerEmbedder(s.embedding_modell)
        e.embed(["Test"] * 32)
        _zeile(True, f"Embeddings {s.embedding_modell} auf {e._m.device}: 32 Texte in {time.monotonic() - t0:.1f} s "
                     "(inkl. Laden)")
    except Exception as ex:  # Modell-Download oder Gerät
        _zeile(False, f"Embeddings: {ex}")
        alles_ok = False

    if s.modell_klein:
        try:
            from gkb.aufbereitung.llm import LiteLLM
            llm = LiteLLM(s.modell_klein, max_tokens=50, api_base=s.modell_klein_api_base)
            t0 = time.monotonic()
            antwort = llm.json('Antworte nur mit JSON: {"ok": true}', "Test")
            _zeile(antwort.get("ok") is True,
                   f"LLM {s.modell_klein}: Antwort in {time.monotonic() - t0:.1f} s")
        except Exception as ex:
            _zeile(False, f"LLM {s.modell_klein}: {ex}")
            alles_ok = False
    else:
        _zeile(None, "GKB_MODELL_KLEIN nicht gesetzt – LLM nicht geprüft")
    return alles_ok
