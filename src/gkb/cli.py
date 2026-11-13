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
    vok.add_argument("--facette", default="thema", choices=["thema", "dokumenttyp"])
    sch = sub.add_parser("schild", help="Textansicht eines Klingelschilds anzeigen")
    sch.add_argument("id")
    sub.add_parser("systemcheck", help="GPU, Docling, Embeddings und LLM-Verbindung prüfen")
    mcp = sub.add_parser("mcp", help="MCP-Server über stdio starten (für Claude, per SSH)")
    mcp.add_argument("--nutzer", required=True, help="Nextcloud-Nutzername; bestimmt die Rechte")
    inv = sub.add_parser("inventur", help="Bestandsaufnahme der Nextcloud-Ordner ohne Download")
    inv.add_argument("--tiefe", type=int, default=1, help="Ordnerebenen für die Aufschlüsselung")
    inv.add_argument("--stichprobe", type=int, default=0,
                     help="N Dateien laden und umwandeln, um Textmenge und Dauer zu messen (nichts wird gespeichert)")
    inv.add_argument("--preis-ein", type=float, help="USD je Mio. Eingabe-Tokens des kleinen Modells")
    inv.add_argument("--preis-aus", type=float, help="USD je Mio. Ausgabe-Tokens des kleinen Modells")
    args = parser.parse_args(argv)

    # Protokoll immer auf stderr: stdout gehört beim MCP-Server dem Protokoll
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=__import__("sys").stderr)
    s = get_settings()

    if args.cmd == "mcp":
        from gkb.abfrage.server import baue_server
        from gkb.abfrage.zugriff import nutzer_laden
        from gkb.aufbereitung.embeddings import LazyEmbedder, SentenceTransformerEmbedder
        nutzer = nutzer_laden(args.nutzer, s)
        logging.info("MCP-Server für %s (admin=%s, %d Principals)", nutzer.id, nutzer.admin, len(nutzer.principals))
        emb = LazyEmbedder(lambda: SentenceTransformerEmbedder(s.embedding_modell, device=s.embedding_geraet_abfrage))
        baue_server(lambda: connect(s.database_url), emb, nutzer).run("stdio")
        return

    if args.cmd == "systemcheck":    # braucht weder Datenbank noch Tresor
        from gkb.systemcheck import pruefen
        raise SystemExit(0 if pruefen(s) else 1)

    if args.cmd == "inventur":       # braucht weder Datenbank noch Tresor
        from gkb.connectors.nextcloud import NextcloudClient
        from gkb.inventur import bericht, erfasse, hochrechnen, stichprobe
        if not s.roots:
            raise SystemExit("GKB_NEXTCLOUD_ROOTS ist leer")
        client = NextcloudClient(s.nextcloud_url, s.nextcloud_user, s.nextcloud_app_password)
        try:
            for root in s.roots:
                inventur = erfasse(client.walk(root), root, args.tiefe)
                messungen = stichprobe(inventur, client, args.stichprobe) if args.stichprobe else []
                print(bericht(inventur, hochrechnen(inventur, messungen, args.preis_ein, args.preis_aus), root))
                print()
        finally:
            client.close()
        return

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
        z = aufbereiten(conn, llm, emb, _pseudonymizer(conn, vault, False), s.kunde_ebene, s.projekt_ebene,
                        s.llm_max_len)
        print(json.dumps(dict(z), ensure_ascii=False))
        return

    if args.cmd == "vokabular":
        from gkb.aufbereitung import vokabular as v
        if args.aktion == "liste":
            for r in conn.execute("SELECT facette, wert, status, anzahl FROM vokabular "
                                  "WHERE facette=%s ORDER BY status, anzahl DESC, wert", (args.facette,)):
                print(f"{r['status']:10} {r['anzahl']:4}  {r['wert']}")
        elif args.aktion == "vorschlagen":
            from gkb.aufbereitung.run import vokabular_vorschlagen
            llm, _ = _modelle(s, False, nur_llm=True)
            for t in vokabular_vorschlagen(conn, llm):
                print("Vorschlag:", t)
        else:
            print(f"{v.freigeben(conn, args.werte or None, args.facette)} Werte ({args.facette}) freigegeben. "
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
    llm = LiteLLM(s.modell_klein, api_base=s.modell_klein_api_base)
    if nur_llm:
        return llm, None
    from gkb.aufbereitung.embeddings import SentenceTransformerEmbedder
    return llm, SentenceTransformerEmbedder(s.embedding_modell)


if __name__ == "__main__":
    main()
