"""Pseudonymisierung mit konsistenten Kürzeln und verschlüsseltem Zuordnungstresor.

Erkennung in drei Schichten:
1. Regeln (E-Mail, Telefon, IBAN)
2. Listen (Ansprechpartner, Einzelunternehmer aus Lexoffice, weitere Namenslisten)
3. Optional NER über Presidio + spaCy (Extra "ner"), falls installiert
Derselbe Klarwert erhält immer dasselbe Kürzel, z. B. [PERSON_0042].
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Iterable, Protocol

from cryptography.fernet import Fernet


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


class Vault(Protocol):
    def get_or_create(self, typ: str, value: str) -> str: ...
    def reveal(self, kuerzel: str) -> str | None: ...


class _VaultBase:
    def __init__(self, fernet_key: str, hmac_key: str):
        if not fernet_key or not hmac_key:
            raise ValueError("GKB_VAULT_KEY und GKB_VAULT_HMAC_KEY müssen gesetzt sein")
        self._fernet = Fernet(fernet_key.encode())
        self._hmac_key = hmac_key.encode()

    def value_hash(self, typ: str, value: str) -> str:
        return hmac.new(self._hmac_key, f"{typ}:{normalize(value)}".encode(), hashlib.sha256).hexdigest()

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode())

    def decrypt(self, token: bytes) -> str:
        return self._fernet.decrypt(bytes(token)).decode()


class MemoryVault(_VaultBase):
    """Für Tests und Trockenläufe."""

    def __init__(self, fernet_key: str, hmac_key: str):
        super().__init__(fernet_key, hmac_key)
        self._by_hash: dict[str, str] = {}
        self._by_kuerzel: dict[str, bytes] = {}
        self._counter: dict[str, int] = {}

    def get_or_create(self, typ: str, value: str) -> str:
        h = self.value_hash(typ, value)
        if h in self._by_hash:
            return self._by_hash[h]
        n = self._counter.get(typ, 0) + 1
        self._counter[typ] = n
        kuerzel = f"[{typ}_{n:04d}]"
        self._by_hash[h] = kuerzel
        self._by_kuerzel[kuerzel] = self.encrypt(value)
        return kuerzel

    def reveal(self, kuerzel: str) -> str | None:
        token = self._by_kuerzel.get(kuerzel)
        return self.decrypt(token) if token else None


class PgVault(_VaultBase):
    """Tresor in Postgres (Tabelle pseudonym)."""

    def __init__(self, conn, fernet_key: str, hmac_key: str):
        super().__init__(fernet_key, hmac_key)
        self.conn = conn

    def get_or_create(self, typ: str, value: str) -> str:
        h = self.value_hash(typ, value)
        row = self.conn.execute("SELECT kuerzel FROM pseudonym WHERE wert_hash = %s", (h,)).fetchone()
        if row:
            return row["kuerzel"]
        # Sperre gegen parallele Vergabe derselben laufenden Nummer
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtext('gkb_pseudonym'))")
        n = self.conn.execute(
            "SELECT count(*) AS n FROM pseudonym WHERE typ = %s", (typ,)
        ).fetchone()["n"] + 1
        kuerzel = f"[{typ}_{n:04d}]"
        self.conn.execute(
            """INSERT INTO pseudonym (kuerzel, typ, wert_hash, wert_verschluesselt)
               VALUES (%s, %s, %s, %s) ON CONFLICT (wert_hash) DO NOTHING""",
            (kuerzel, typ, h, self.encrypt(value)),
        )
        row = self.conn.execute("SELECT kuerzel FROM pseudonym WHERE wert_hash = %s", (h,)).fetchone()
        return row["kuerzel"]

    def reveal(self, kuerzel: str) -> str | None:
        row = self.conn.execute(
            "SELECT wert_verschluesselt FROM pseudonym WHERE kuerzel = %s", (kuerzel,)
        ).fetchone()
        return self.decrypt(row["wert_verschluesselt"]) if row else None


@dataclass(frozen=True)
class Treffer:
    start: int
    end: int
    typ: str
    text: str


RULES: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")),
    # Telefonnummern: +49 / 0 am Anfang, mindestens 8 Ziffern insgesamt
    ("TELEFON", re.compile(r"(?<![\w/])(?:\+\d{2}|0)[\d\s/().-]{7,}\d")),
]


DATUM = re.compile(r"\b\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})\b")   # Datumsangaben sind keine Telefonnummern


class Pseudonymizer:
    def __init__(self, vault: Vault, namen: Iterable[str] = (), use_ner: bool = False):
        self.vault = vault
        namen = sorted({n.strip() for n in namen if n and len(n.strip()) >= 3}, key=len, reverse=True)
        self._namen_re = (
            re.compile(r"\b(?:" + "|".join(re.escape(n) for n in namen) + r")\b", re.IGNORECASE)
            if namen else None
        )
        self._ner = _load_ner() if use_ner else None

    def find(self, text: str) -> list[Treffer]:
        hits: list[Treffer] = []
        for typ, pattern in RULES:
            for m in pattern.finditer(text):
                if typ == "TELEFON" and (sum(c.isdigit() for c in m.group()) < 8 or DATUM.search(m.group())):
                    continue
                hits.append(Treffer(m.start(), m.end(), typ, m.group()))
        if self._namen_re:
            for m in self._namen_re.finditer(text):
                hits.append(Treffer(m.start(), m.end(), "PERSON", m.group()))
        if self._ner:
            hits.extend(self._ner(text))
        return _resolve_overlaps(hits)

    def pseudonymize(self, text: str) -> str:
        out, pos = [], 0
        for t in self.find(text):
            out.append(text[pos:t.start])
            out.append(self.vault.get_or_create(t.typ, t.text))
            pos = t.end
        out.append(text[pos:])
        return "".join(out)


def _resolve_overlaps(hits: list[Treffer]) -> list[Treffer]:
    """Längster Treffer gewinnt, keine Überlappungen."""
    chosen: list[Treffer] = []
    for t in sorted(hits, key=lambda h: (-(h.end - h.start), h.start)):
        if all(t.end <= c.start or t.start >= c.end for c in chosen):
            chosen.append(t)
    return sorted(chosen, key=lambda h: h.start)


def _load_ner():
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
    except ImportError:  # Extra "ner" nicht installiert
        return None
    provider = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "de", "model_name": "de_core_news_lg"}],
    })
    engine = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["de"])

    def run(text: str) -> list[Treffer]:
        return [
            Treffer(r.start, r.end, "PERSON", text[r.start:r.end])
            for r in engine.analyze(text=text, language="de", entities=["PERSON"])
            if r.score >= 0.6
        ]

    return run
