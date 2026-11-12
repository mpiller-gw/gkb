"""Einlese-Pipeline: Nextcloud -> Lieferbeleg -> Original -> Markdown -> Pseudonymisierung -> Rohdokument.

Duplikaterkennung in Baustein 1:
  Stufe 1  gleiche fileid + ETag schon verarbeitet            -> übersprungen
  Stufe 2  gleicher Inhalt (SHA-256) unter anderer fileid     -> 'duplikat', nur weitere Herkunft
  sonst    gleiche fileid, gleicher Inhalt, neuer Pfad/ETag   -> 'verschoben'
Stufen 3 und 4 (Fast-Duplikate, semantische Dubletten) folgen in Baustein 2.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

from psycopg.types.json import Jsonb

from gkb.connectors.nextcloud import NextcloudClient, RemoteFile
from gkb.ingest.parse import is_supported, to_markdown
from gkb.ingest.pseudonymize import Pseudonymizer
from gkb.connectors.nextcloud_rechte import PfadRechte, speichere_partition
from gkb.storage import store_original

log = logging.getLogger(__name__)
PROZESS = "ingest-nextcloud-v1"


def rechte_partition(path: str, root: str, depth: int) -> str:
    """Pfad-Fallback (ohne Nextcloud-Rechte): Wurzelordner + die ersten `depth` Ordnerebenen."""
    return PfadRechte(depth).partition(path, root).id


@dataclass(frozen=True)
class Entscheidung:
    status: str          # neu | update | verschoben | duplikat
    verarbeiten: bool    # muss geparst und pseudonymisiert werden


def entscheide(bestehend_sha: str | None, sha: str, duplikat_von: int | None) -> Entscheidung:
    if bestehend_sha is not None and bestehend_sha == sha:
        return Entscheidung("verschoben", False)
    if bestehend_sha is None and duplikat_von is not None:
        return Entscheidung("duplikat", False)
    return Entscheidung("update" if bestehend_sha is not None else "neu", True)


def sync_nextcloud(conn, client: NextcloudClient, pseudo: Pseudonymizer, quelle_id: int,
                   root: str, storage_dir: str, rechte) -> Counter:
    """`rechte`: NextcloudRechte (Freigaben + Gruppenordner) oder PfadRechte (Fallback)."""
    zaehler: Counter = Counter()
    lauf = conn.execute("INSERT INTO sync_lauf (quelle_id) VALUES (%s) RETURNING id", (quelle_id,)).fetchone()["id"]
    conn.commit()
    gesehen: set[str] = set()

    for f in client.walk(root):
        gesehen.add(f.fileid)
        if not is_supported(f.path):
            zaehler["nicht_unterstuetzt"] += 1
            continue
        if conn.execute(
            """SELECT 1 FROM lieferbeleg WHERE quelle_id=%s AND externe_id=%s AND version=%s
               AND status <> 'fehler'""",
            (quelle_id, f.fileid, f.etag),
        ).fetchone():
            zaehler["unveraendert"] += 1
            # Rechte können sich ändern, ohne dass sich die Datei ändert
            neu = speichere_partition(conn, rechte.partition(f.path, root))
            if conn.execute(
                """UPDATE rohdokument SET rechte_partition=%s, aktualisiert=now()
                   WHERE quelle_id=%s AND externe_id=%s AND rechte_partition <> %s""",
                (neu, quelle_id, f.fileid, neu),
            ).rowcount:
                zaehler["rechte_geaendert"] += 1
            conn.commit()
            continue
        try:
            status = _verarbeite(conn, client, pseudo, quelle_id, f, root, storage_dir, rechte)
            conn.commit()
        except Exception as e:  # eine fehlerhafte Datei darf den Lauf nicht abbrechen
            conn.rollback()
            log.exception("Fehler bei %s", f.path)
            conn.execute(
                """INSERT INTO lieferbeleg (quelle_id, externe_id, version, pfad, mime, groesse,
                       geaendert, status, fehler)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'fehler',%s) ON CONFLICT DO NOTHING""",
                (quelle_id, f.fileid, f.etag, f.path, f.mime, f.size, f.modified, str(e)[:2000]),
            )
            conn.commit()
            status = "fehler"
        zaehler[status] += 1

    # Nicht mehr vorhandene Dateien markieren (Löschung über Lineage folgt in Baustein 2)
    geloescht = conn.execute(
        """UPDATE rohdokument SET geloescht = true, aktualisiert = now()
           WHERE quelle_id = %s AND NOT geloescht AND (pfad = %s OR pfad LIKE %s)
             AND NOT (externe_id = ANY(%s))""",
        (quelle_id, root, f"{root}/%", list(gesehen)),
    ).rowcount
    zaehler["geloescht"] += geloescht
    conn.execute("UPDATE sync_lauf SET ende = now(), zaehler = %s WHERE id = %s", (Jsonb(dict(zaehler)), lauf))
    conn.commit()
    return zaehler


def _verarbeite(conn, client, pseudo, quelle_id, f: RemoteFile, root, storage_dir, rechte) -> str:
    data = client.download(f.path)
    sha, ablage = store_original(storage_dir, data, PurePosixPath(f.path).suffix.lower())
    bestehend = conn.execute(
        "SELECT id, sha256 FROM rohdokument WHERE quelle_id=%s AND externe_id=%s", (quelle_id, f.fileid)
    ).fetchone()
    andere = conn.execute(
        """SELECT id FROM rohdokument WHERE sha256=%s AND NOT geloescht
           AND NOT (quelle_id=%s AND externe_id=%s) LIMIT 1""",
        (sha, quelle_id, f.fileid),
    ).fetchone()
    e = entscheide(bestehend["sha256"] if bestehend else None, sha, andere["id"] if andere else None)

    deep_link = client.deep_link(f.fileid)
    beleg_id = conn.execute(
        """INSERT INTO lieferbeleg (quelle_id, externe_id, version, sha256, pfad, deep_link, mime,
               groesse, geaendert, status, ablage_pfad)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (quelle_id, externe_id, version) DO UPDATE SET
               sha256=EXCLUDED.sha256, status=EXCLUDED.status, ablage_pfad=EXCLUDED.ablage_pfad,
               fehler=NULL, empfangen=now()
           RETURNING id""",
        (quelle_id, f.fileid, f.etag, sha, f.path, deep_link, f.mime, f.size, f.modified, e.status, ablage),
    ).fetchone()["id"]

    partition = speichere_partition(conn, rechte.partition(f.path, root))
    titel = pseudo.pseudonymize(PurePosixPath(f.path).stem)

    if e.status == "duplikat":
        conn.execute("INSERT INTO dokument_herkunft VALUES (%s, %s) ON CONFLICT DO NOTHING",
                     (andere["id"], beleg_id))
        return e.status

    if e.status == "verschoben":
        conn.execute(
            """UPDATE rohdokument SET beleg_id=%s, pfad=%s, titel=%s, rechte_partition=%s,
                   geloescht=false, aktualisiert=now() WHERE id=%s""",
            (beleg_id, f.path, titel, partition, bestehend["id"]),
        )
        return e.status

    markdown = to_markdown(data, f.path, f.mime)
    markdown_pseudo = pseudo.pseudonymize(markdown)
    doc_id = conn.execute(
        """INSERT INTO rohdokument (quelle_id, externe_id, beleg_id, titel, pfad, deep_link, mime,
               sha256, markdown_pseudo, rechte_partition)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (quelle_id, externe_id) DO UPDATE SET
               beleg_id=EXCLUDED.beleg_id, titel=EXCLUDED.titel, pfad=EXCLUDED.pfad,
               mime=EXCLUDED.mime, sha256=EXCLUDED.sha256, markdown_pseudo=EXCLUDED.markdown_pseudo,
               rechte_partition=EXCLUDED.rechte_partition, geloescht=false, aktualisiert=now()
           RETURNING id""",
        (quelle_id, f.fileid, beleg_id, titel, f.path, deep_link, f.mime, sha, markdown_pseudo, partition),
    ).fetchone()["id"]
    conn.execute(
        """INSERT INTO lineage (abgeleitet_typ, abgeleitet_id, eingabe_typ, eingabe_id, prozess)
           VALUES ('rohdokument', %s, 'lieferbeleg', %s, %s) ON CONFLICT DO NOTHING""",
        (str(doc_id), str(beleg_id), PROZESS),
    )
    return e.status
