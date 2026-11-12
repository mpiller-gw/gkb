"""Kommandozeile: gkb init-db | import-kunden <xlsx> | sync | aufbereiten | vokabular ... | schild <id>"""
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
    auf = sub.add_parser("aufbereiten", help="Abschnitte, Tags, Klingelschilder, Cluster, Faktentabelle")
    auf.add_argument("--trocken", action="store_true",
                     help="ohne echtes LLM/Embedding (Platzhalter, nur zum Testen des Ablaufs)")
    vok = sub.add_parser("vokabular", help="Themenvokabular verwalten")
    vok.add_argument("aktion", choices=["liste", "vorschlagen", "freigeben"])
    vok.add_argument("werte", nargs="*", help="bei 'freigeben': einzelne Werte (ohne Angabe: alle Vorschläge)")
    sch = sub.add_parser("schild", help="Textansicht eines Klingelschilds anzeigen")
    sch.add_argument("id")
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
        from gkb.connectors.nextcloud_rechte import NextcloudRechte, PfadRechte
        from gkb.ingest.pipeline import sync_nextcloud

        if not s.roots:
            raise SystemExit("GKB_NEXTCLOUD_ROOTS ist leer")
        pseudo = _pseudonymizer(conn, vault, args.ner)
        client = NextcloudClient(s.nextcloud_url, s.nextcloud_user, s.nextcloud_app_password)
        rechte = (
            PfadRechte(s.partition_depth) if s.rechte_modus == "pfad" else
            NextcloudRechte(s.nextcloud_url, s.nextcloud_user, s.nextcloud_app_password,
                            s.nextcloud_admin_user, s.nextcloud_admin_password)
        )
        qid = quelle_id(conn, "nextcloud", f"nextcloud:{s.nextcloud_url}", {"roots": s.roots})
        try:
            for root in s.roots:
                z = sync_nextcloud(conn, client, pseudo, qid, root, s.storage_dir, rechte)
                print(f"{root}: {json.dumps(dict(z), ensure_ascii=False)}")
        finally:
            client.close()
        unvollstaendig = conn.execute(
            "SELECT count(*) AS n FROM rechte_partition WHERE NOT vollstaendig").fetchone()["n"]
        if unvollstaendig:
            print(f"Hinweis: {unvollstaendig} Rechte-Partition(en) unvollständig – nur für Administratoren sichtbar.")
        return

    if args.cmd == "aufbereiten":
        from gkb.aufbereitung.run import aufbereiten
        llm, emb = _modelle(s, args.trocken)
        z = aufbereiten(conn, llm, emb, _pseudonymizer(conn, vault, False), s.kunde_ebene, s.projekt_ebene)
        print(json.dumps(dict(z), ensure_ascii=False))
        return

    if args.cmd == "vokabular":
        from gkb.aufbereitung import vokabular as v
        if args.aktion == "liste":
            for r in conn.execute("SELECT facette, wert, status, anzahl FROM vokabular "
                                  "WHERE facette='thema' ORDER BY status, anzahl DESC, wert"):
                print(f"{r['status']:10} {r['anzahl']:4}  {r['wert']}")
        elif args.aktion == "vorschlagen":
            from gkb.aufbereitung.run import vokabular_vorschlagen
            llm, _ = _modelle(s, False, nur_llm=True)
            for t in vokabular_vorschlagen(conn, llm):
                print("Vorschlag:", t)
        else:
            print(f"{v.freigeben(conn, args.werte or None)} Themen freigegeben. "
                  "Nächstes 'gkb aufbereiten' vergibt sie als Tags (ohne LLM-Kosten).")
        return

    if args.cmd == "schild":
        row = conn.execute("SELECT text_ansicht FROM klingelschild WHERE id=%s", (args.id,)).fetchone()
        print(row["text_ansicht"] if row else "Nicht gefunden.")


def _pseudonymizer(conn, vault, ner: bool):
    from gkb.ingest.kunden import load_namensliste
    from gkb.ingest.pseudonymize import Pseudonymizer
    return Pseudonymizer(vault, load_namensliste(conn, vault), use_ner=ner)


def _modelle(s, trocken: bool, nur_llm: bool = False):
    if trocken:
        from gkb.aufbereitung.embeddings import HashEmbedder
        from gkb.aufbereitung.platzhalter import PlatzhalterLLM
        return PlatzhalterLLM(), HashEmbedder()
    if not s.modell_klein:
        raise SystemExit("GKB_MODELL_KLEIN ist nicht gesetzt (oder --trocken verwenden)")
    from gkb.aufbereitung.llm import LiteLLM
    llm = LiteLLM(s.modell_klein)
    if nur_llm:
        return llm, None
    from gkb.aufbereitung.embeddings import SentenceTransformerEmbedder
    return llm, SentenceTransformerEmbedder(s.embedding_modell)


if __name__ == "__main__":
    main()
