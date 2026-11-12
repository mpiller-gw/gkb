"""Dokumente in Markdown umwandeln (inkl. Tabellen). Nutzt Docling, wenn installiert."""
from __future__ import annotations

import tempfile
from pathlib import Path

TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "text/vtt"}
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".xlsx", ".pptx", ".md", ".txt", ".csv", ".vtt", ".html", ".htm"}


class ParseError(Exception):
    pass


def is_supported(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def to_markdown(data: bytes, filename: str, mime: str = "") -> str:
    suffix = Path(filename).suffix.lower()
    if mime in TEXT_TYPES or suffix in {".md", ".txt", ".csv", ".vtt"}:
        return data.decode("utf-8", errors="replace")
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as e:  # Extra "parse" fehlt
        raise ParseError("Docling nicht installiert (pip install '.[parse]')") from e
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            doc = _converter(DocumentConverter).convert(tmp.name).document
        except Exception as e:  # Docling wirft je nach Format unterschiedliche Fehler
            raise ParseError(f"{filename}: {e}") from e
    return doc.export_to_markdown()


_CONVERTER = None


def _converter(cls):
    global _CONVERTER
    if _CONVERTER is None:
        _CONVERTER = cls()
    return _CONVERTER
