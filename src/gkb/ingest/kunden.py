"""Import der Kundenliste aus einem Lexoffice-Excel-Export.

Die Spaltennamen des Exports werden über Kandidatenlisten erkannt. Passt ein Name nicht,
kann er über das Argument `spalten` explizit gesetzt werden.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook

KANDIDATEN = {
    "kundennummer": ["kundennummer", "kunden-nr.", "kunden-nr", "kundennr", "kundennr.", "nummer"],
    "firma": ["firma", "firmenname", "unternehmen", "organisation", "name"],
    "vorname": ["vorname"],
    "nachname": ["nachname"],
    "ap_vorname": ["ansprechpartner vorname", "kontaktperson vorname"],
    "ap_nachname": ["ansprechpartner nachname", "kontaktperson nachname"],
    "ansprechpartner": ["ansprechpartner", "kontaktperson"],
}

FREEMAIL = {"gmail.com", "googlemail.com", "gmx.de", "gmx.net", "web.de", "t-online.de",
            "outlook.com", "hotmail.com", "yahoo.com", "yahoo.de", "icloud.com", "freenet.de"}


@dataclass
class KundeRecord:
    kundennummer: str
    name: str
    ist_person: bool
    domains: list[str] = field(default_factory=list)
    personen: list[str] = field(default_factory=list)   # Namen, die pseudonymisiert werden
    emails: list[str] = field(default_factory=list)


def _clean(v) -> str:
    return str(v).strip() if v is not None else ""


def read_lexoffice(path: str | Path, spalten: dict[str, str] | None = None) -> list[KundeRecord]:
    ws = load_workbook(path, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    # Kopfzeile = erste Zeile mit einer Kundennummer-Spalte
    header_idx = next(
        (i for i, r in enumerate(rows[:20])
         if any(_clean(c).lower() in KANDIDATEN["kundennummer"] for c in r)),
        None,
    )
    if header_idx is None:
        raise ValueError("Keine Kopfzeile mit Kundennummer gefunden – Spalten bitte über 'spalten' angeben")
    header = [_clean(c).lower() for c in rows[header_idx]]

    def col(key: str) -> int | None:
        if spalten and key in spalten:
            name = spalten[key].lower()
            return header.index(name) if name in header else None
        for cand in KANDIDATEN[key]:
            if cand in header:
                return header.index(cand)
        return None

    idx = {k: col(k) for k in KANDIDATEN}
    email_cols = [i for i, h in enumerate(header) if "mail" in h]
    if idx["kundennummer"] is None:
        raise ValueError("Spalte Kundennummer nicht gefunden")

    def get(r, key):
        i = idx[key]
        return _clean(r[i]) if i is not None and i < len(r) else ""

    result: list[KundeRecord] = []
    for r in rows[header_idx + 1:]:
        nr = get(r, "kundennummer")
        if not nr:
            continue
        firma = get(r, "firma")
        person = " ".join(x for x in (get(r, "vorname"), get(r, "nachname")) if x)
        ap = get(r, "ansprechpartner") or " ".join(
            x for x in (get(r, "ap_vorname"), get(r, "ap_nachname")) if x)
        ist_person = not firma and bool(person)
        name = firma or person
        if not name:
            continue
        emails = [_clean(r[i]) for i in email_cols if i < len(r) and "@" in _clean(r[i])]
        domains = sorted({e.rsplit("@", 1)[1].lower() for e in emails} - FREEMAIL)
        personen = sorted({p for p in (person, ap) if p})
        result.append(KundeRecord(nr, name, ist_person, domains, personen, emails))
    return result


def save_kunden(conn, vault, records: list[KundeRecord]) -> int:
    """Kunden speichern; Personen und E-Mails landen verschlüsselt in der Personenliste."""
    for k in records:
        conn.execute(
            """INSERT INTO kunde (kundennummer, name, ist_person, domains)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (kundennummer) DO UPDATE
               SET name = EXCLUDED.name, ist_person = EXCLUDED.ist_person,
                   domains = EXCLUDED.domains, aktualisiert = now()""",
            # Bei Einzelunternehmern ist der Kundenname selbst ein Personenname
            (k.kundennummer, vault.get_or_create("PERSON", k.name) if k.ist_person else k.name,
             k.ist_person, k.domains),
        )
        for typ, werte in (("PERSON", k.personen), ("EMAIL", k.emails)):
            for w in werte:
                conn.execute(
                    """INSERT INTO personenliste (wert_hash, wert_verschluesselt, typ, herkunft)
                       VALUES (%s, %s, %s, 'lexoffice') ON CONFLICT (wert_hash) DO NOTHING""",
                    (vault.value_hash(typ, w), vault.encrypt(w), typ),
                )
    conn.commit()
    return len(records)


def load_namensliste(conn, vault) -> list[str]:
    """Entschlüsselte Namen für den Listen-Erkenner (nur im Speicher des Prozesses)."""
    rows = conn.execute("SELECT wert_verschluesselt FROM personenliste WHERE typ = 'PERSON'").fetchall()
    return [vault.decrypt(r["wert_verschluesselt"]) for r in rows]
