# GKB – KI-Wissensschicht (Prototyp)

Konzept: [GKB – Konzept: KI-Wissensschicht](https://claude.ai/artifact/Ap4L6vt1gfWVJiQj6cfwY3)

## Stand: Baustein 1 – Einlesen

- **Nextcloud-Konnektor** (WebDAV, nur lesend): stabile `fileid` als externe ID, ETag als Version, Deep-Link `/f/<fileid>`
- **Lieferbeleg** je Dateistand, Original unverändert in der Ablage (nach SHA-256)
- **Duplikaterkennung** Stufe 1 (fileid + ETag) und Stufe 2 (gleicher Inhalt); verschobene/umbenannte Dateien werden erkannt
- **Markdown** aus PDF/Office über Docling (inkl. Tabellen, OCR)
- **Pseudonymisierung** mit konsistenten Kürzeln (`[PERSON_0001]`), verschlüsseltem Tresor, Regeln (E-Mail, Telefon, IBAN), Namensliste aus Lexoffice, optional NER (Presidio + spaCy)
- **Kundenliste** aus Lexoffice-Excel (Kundennummer = Kunden-ID, E-Mail-Domains für die Meeting-Zuordnung)
- **Lineage** Rohdokument ← Lieferbeleg; gelöschte Dateien werden markiert

Pilot-Vereinfachung: Die Rechte-Partition ergibt sich aus dem Ordnerpfad (`GKB_PARTITION_DEPTH`).
Das Auslesen von Freigaben und Gruppenordner-ACLs folgt.

## Einrichtung auf dem Server (Hetzner, Docker)

```bash
git clone <repo> gkb && cd gkb
cp .env.example .env          # Werte eintragen, Schlüssel erzeugen (siehe Kommentare)
chmod 600 .env
docker compose up -d db
docker compose run --rm app init-db
```

Kundenliste importieren (Export nach `./import/` legen):

```bash
docker compose run --rm app import-kunden /import/kunden.xlsx
```

Nextcloud einlesen:

```bash
docker compose run --rm app sync          # mit NER: WITH_NER=true docker compose build app; ... sync --ner
```

Die Ausgabe zeigt je Ordner: `neu`, `update`, `verschoben`, `duplikat`, `unveraendert`, `nicht_unterstuetzt`, `fehler`, `geloescht`.

## Entwicklung

```bash
pip install -e ".[dev]"
pytest                                   # Unit-Tests
GKB_TEST_DATABASE_URL=postgresql://gkb:gkb@localhost:5432/gkb pytest   # inkl. DB-Integrationstest
```

## Nächste Bausteine

2. Aufbereiten: Tags, Klingelschilder, Cluster-Schilder, Faktentabelle Angebotspositionen, Embeddings
3. Abfragen: vier Vergleichsvarianten, MCP-Server
4. Messen: Messumgebung über das Evaluationsset
