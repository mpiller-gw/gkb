"""Kommandozeile: gkb init-db | gkb import-kunden <xlsx> | gkb sync"""
from __future__ import annotations

import argparse
import json
import logging

from gkb.config import get_settings
from gkb.db import connect, init_db, quelle_id


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gkb")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="Datenbankschema anlegen bzw. aktualisieren")
    imp = sub.add_parser("import-kunden", help="Kundenliste aus Lexoffice-Export (xlsx) importieren")
    imp.add_argument("datei")
    sync = sub.add_parser("sync", help="Nextcloud-Ordner einlesen")
    sync.add_argument("--ner", action="store_true", help="zusätzlich NER-Erkennung (Extra 'ner')")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = get_settings()
    conn = connect(s.database_url)

    if args.cmd == "init-db":
        init_db(conn)
        print("Schema ist aktuell.")
        return

    from gkb.ingest.pseudonymize import PgVault
    vault = PgVault(conn, s.vault_key, s.vault_hmac_key)

    if args.cmd == "import-kunden":
        from gkb.ingest.kunden import read_lexoffice, save_kunden
        n = save_kunden(conn, vault, read_lexoffice(args.datei))
        print(f"{n} Kunden importiert.")
        return

    if args.cmd == "sync":
        from gkb.connectors.nextcloud import NextcloudClient
        from gkb.ingest.kunden import load_namensliste
        from gkb.ingest.pipeline import sync_nextcloud
        from gkb.ingest.pseudonymize import Pseudonymizer

        if not s.roots:
            raise SystemExit("GKB_NEXTCLOUD_ROOTS ist leer")
        pseudo = Pseudonymizer(vault, load_namensliste(conn, vault), use_ner=args.ner)
        client = NextcloudClient(s.nextcloud_url, s.nextcloud_user, s.nextcloud_app_password)
        qid = quelle_id(conn, "nextcloud", f"nextcloud:{s.nextcloud_url}", {"roots": s.roots})
        try:
            for root in s.roots:
                z = sync_nextcloud(conn, client, pseudo, qid, root, s.storage_dir, s.partition_depth)
                print(f"{root}: {json.dumps(dict(z), ensure_ascii=False)}")
        finally:
            client.close()


if __name__ == "__main__":
    main()
