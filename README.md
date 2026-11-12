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

## Stand: Baustein 1b – Rechte

- **Rechte-Partitionen** aus Nextcloud: Freigaben an Nutzer und Gruppen (vererbt über die Ordner),
  Eigentümer, Gruppenordner mit ACL-Regeln (tiefste Regel gewinnt, auch Verbote einzelner Nutzer)
- Linkfreigaben, E-Mail- und föderierte Freigaben geben in GKB **keine** Rechte
- Nicht lesbare Rechte → Partition `vollstaendig = false` → später nur für Administratoren sichtbar
- Rechteänderungen ohne Dateiänderung werden beim nächsten `sync` erkannt
- Fallback `GKB_RECHTE_MODUS=pfad`: Partition aus dem Ordnerpfad (`GKB_PARTITION_DEPTH`)

> Gegen eine echte Nextcloud noch nicht getestet. Vor allem zu prüfen: Welche Freigaben sieht das
> Dienstkonto über die Share-API, und ist `nc:acl-list` für das Dienstkonto lesbar?

## Stand: Baustein 2 – Aufbereitung

- **Abschnitte** (Stufe 3) entlang der Überschriften, mit Embedding und deutschem Volltextindex
- **Tags** (Stufe 1) aus kontrolliertem Vokabular; neue Themen landen als Vorschlag
  (`gkb vokabular liste | vorschlagen | freigeben`), freigegebene Themen werden ohne LLM-Kosten nachgetragen
- **Klingelschilder** (Stufe 2) je Dokument: gespeicherte Form (JSON) und kompakte Textansicht fürs LLM,
  Kunde und Projekt aus dem Ordnerpfad, Lexoffice-Kundennummer als ID, Pseudonym-Kürzel als Personen
- **Cluster-Schilder** Projekt → Kunde → Wissensraum, je Rechte-Partition
- **Faktentabelle** `angebot` / `angebotsposition` mit Plausibilitätsprüfung (Summe = Gesamt ± 1 EUR),
  Office- und PDF-Fassung als Gruppe, Sicht `angebot_massgeblich` (PDF vor Office)
- **Inkrementell**: unveränderte Dokumente kosten keinen LLM-Aufruf; Rechteänderungen werden ohne LLM
  umgehängt; gelöschte Dokumente räumen Schilder, Abschnitte und Fakten ab
- Prompt-Injection-Schutz: Dokumente gehen als Daten in Tags, Antworten werden gegen Schemas geprüft

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

Die Ausgabe zeigt je Ordner: `neu`, `update`, `verschoben`, `duplikat`, `unveraendert`, `rechte_geaendert`,
`nicht_unterstuetzt`, `fehler`, `geloescht`.

Aufbereiten (Tags, Klingelschilder, Cluster, Fakten):

```bash
docker compose run --rm app aufbereiten --trocken   # Ablauf ohne LLM testen
docker compose run --rm app aufbereiten             # mit GKB_MODELL_KLEIN und lokalen Embeddings
docker compose run --rm app vokabular vorschlagen   # Startvokabular aus dem Bestand
docker compose run --rm app vokabular liste
docker compose run --rm app vokabular freigeben     # alle Vorschläge (oder einzelne Werte angeben)
docker compose run --rm app schild ke-42            # Textansicht eines Klingelschilds
```

## Entwicklung

```bash
pip install -e ".[dev]"
pytest                                   # Unit-Tests
GKB_TEST_DATABASE_URL=postgresql://gkb:gkb@localhost:5432/gkb pytest   # inkl. DB-Integrationstest
```

## Nächste Bausteine

2b. Duplikate Stufe 3 und 4 (Fast-Duplikate, semantische Dubletten)
3. Abfragen: vier Vergleichsvarianten, MCP-Server
4. Messen: Messumgebung über das Evaluationsset
