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
- **Tags** (Stufe 1) aus kontrolliertem Vokabular; neue Themen und Dokumenttypen landen als Vorschlag
  (`gkb vokabular liste|vorschlagen|freigeben [--facette thema|dokumenttyp]`), freigegebene Werte werden
  ohne LLM-Kosten nachgetragen. Vorbelegt sind allgemeine Unternehmensdokumente (Angebot, Vertrag, Rechnung,
  Richtlinie, Prozessbeschreibung, Bericht, Personal-/Finanzunterlage …)
- **Klingelschilder** (Stufe 2) je Dokument: gespeicherte Form (JSON) und kompakte Textansicht fürs LLM,
  Kunde und Projekt aus dem Ordnerpfad, Lexoffice-Kundennummer als ID, Pseudonym-Kürzel als Personen
- **Cluster-Schilder** Projekt → Kunde → Wissensraum, je Rechte-Partition
- **Faktentabelle** `angebot` / `angebotsposition` mit Plausibilitätsprüfung (Summe = Gesamt ± 1 EUR),
  Office- und PDF-Fassung als Gruppe, Sicht `angebot_massgeblich` (PDF vor Office)
- **Inkrementell**: unveränderte Dokumente kosten keinen LLM-Aufruf; Rechteänderungen werden ohne LLM
  umgehängt; gelöschte Dokumente räumen Schilder, Abschnitte und Fakten ab
- Prompt-Injection-Schutz: Dokumente gehen als Daten in Tags, Antworten werden gegen Schemas geprüft

## Einrichtung auf dem Server (Hetzner, Docker)

Einmalig als root auf einem neuen Ubuntu-LTS-Server (Standort Deutschland):

```bash
git clone https://github.com/mpiller-gw/gkb.git /opt/gkb && cd /opt/gkb
ADMIN_CIDR=<eure-IP>/32 bash scripts/server-setup.sh      # Updates, Benutzer gkb, SSH-Härtung, ufw, Docker, Backup-Timer
bash scripts/volume-verschluesseln.sh /dev/disk/by-id/scsi-0HC_Volume_<id>   # LUKS, eingehängt unter /srv/gkb-data
chown -R gkb:gkb /opt/gkb
```

Danach als Benutzer `gkb`:

```bash
cd /opt/gkb
cp .env.example .env && chmod 600 .env    # Werte eintragen, Passwörter und Tresor-Schlüssel erzeugen
docker compose up -d db
docker compose run --rm app init-db
```

Nach einem Neustart des Servers: `sudo bash scripts/volume-verschluesseln.sh --entsperren <gerät>`,
dann `docker compose up -d db`. Die Passphrase liegt bewusst nicht auf dem Server.

**GPU (Hetzner GEX44, RTX 4000 SFF Ada 20 GB):**

```bash
sudo bash scripts/gpu-setup.sh          # Treiber + NVIDIA Container Toolkit, danach Neustart
# in .env:  COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
docker compose build app
docker compose run --rm --no-deps app systemcheck      # GPU, Docling, Embeddings, LLM prüfen
```

Mit GPU laufen Docling (Layout, OCR) und die Embeddings auf der Grafikkarte. Optional ein lokales
Sprachmodell über vLLM als eigener Container (nur im internen Netz):

```bash
# in .env: GKB_MODELL_KLEIN=hosted_vllm/gkb-lokal und GKB_MODELL_KLEIN_API_BASE=http://llm:8000/v1
docker compose --profile lokal up -d llm     # lädt beim ersten Start das Modell (GKB_LLM_MODELL)
docker compose logs -f llm                   # warten bis "Application startup complete"
docker compose run --rm --no-deps app systemcheck
```

GPU-Speicher teilen sich vLLM (`GKB_LLM_GPU_ANTEIL`, Standard 60 %) und Docling/Embeddings.
Für große Sync-Läufe vLLM vorher stoppen (`docker compose stop llm`).

**Backup:** täglich 02:30 über `gkb-backup.timer` – Datenbank-Dump und Originale, verschlüsselt mit
restic, z. B. auf eine Hetzner Storage Box (`/etc/gkb/backup.env`). Nicht im Backup und deshalb
getrennt außerhalb des Servers zu sichern: `.env` (Tresor-Schlüssel), restic-Passwort, LUKS-Passphrase.

Kundenliste importieren (Export nach `./import/` legen):

```bash
docker compose run --rm app import-kunden /import/kunden.xlsx
```

Bestandsaufnahme vor dem ersten Einlesen (nur Dateiliste, kein Download, keine Datenbank nötig):

```bash
docker compose run --rm --no-deps app inventur                     # Anzahl/Größe je Unterordner und Dateityp
docker compose run --rm --no-deps app inventur --stichprobe 30 \
    --preis-ein <USD je Mio. Tokens> --preis-aus <USD je Mio. Tokens>   # misst Textmenge und Dauer, rechnet hoch
```

Die Stichprobe lädt und wandelt N zufällige Dateien nur im Arbeitsspeicher um; nichts wird gespeichert
oder an ein LLM gesendet. Tipp: `GKB_NEXTCLOUD_ROOTS` zuerst auf einen Ausschnitt setzen (z. B. `Sales/2025`).

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

## Stand: Baustein 3 – Abfrage über Claude (MCP)

- **Werkzeuge:** `tags_uebersicht` (Stufe 1), `klingelschilder_suchen` (Stufe 2, hybrid: Vektor + Volltext,
  mit Filtern), `details_lesen` (Stufe 3, nur passende Abschnitte bis zum Token-Budget), `verweisen_folgen`,
  `angebote_abfragen` (Datenpfad, exakte Werte, maßgebliche Fassung, Positionen aus Office)
- **Rechte vor jeder Suche:** sichtbar ist, was einer Gruppe/dem Nutzer erlaubt und nicht verboten ist;
  unvollständige Rechte nur für `GKB_ADMINS`. Gruppen des Nutzers kommen über den Nextcloud-Admin-Zugang.
- **Pseudonymisiert:** Claude erhält nur Auszüge mit Kürzeln wie `[PERSON_0042]`; Klarnamen bleiben auf dem Server.
- **Protokoll:** jede Abfrage mit Nutzer und Werkzeug (stderr, Standard `/tmp/gkb-mcp.log`).

### Einrichtung für eine Person (Beispiel: michael)

1. Auf dem eigenen Rechner einen eigenen Schlüssel nur für GKB erzeugen:
   `ssh-keygen -t ed25519 -f ~/.ssh/gkb_mcp -C michael-gkb`
2. Auf dem Server in `/home/gkb/.ssh/authorized_keys` eine Zeile mit erzwungenem Befehl ergänzen:
   ```
   command="/opt/gkb/scripts/mcp.sh michael",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... michael-gkb
   ```
   Der Name nach `mcp.sh` ist der Nextcloud-Nutzername. Für Vollzugriff zusätzlich `GKB_ADMINS=michael` in `.env`.
3. Einmal manuell verbinden, um den Server-Schlüssel zu bestätigen (sonst scheitert Claude an der Rückfrage):
   `ssh -i ~/.ssh/gkb_mcp gkb@<server>` – die Verbindung bleibt stumm; mit Strg+C beenden.
4. In Claude Desktop (*Einstellungen → Entwickler → Konfiguration bearbeiten*) eintragen:
   ```json
   {
     "mcpServers": {
       "gkb": {
         "command": "ssh",
         "args": ["-T", "-i", "/Users/michael/.ssh/gkb_mcp", "gkb@<server>"]
       }
     }
   }
   ```
   Unter Windows den Pfad als `C:\\Users\\<name>\\.ssh\\gkb_mcp` angeben. Claude Desktop neu starten.
5. Die IP des Rechners muss in der Firewall freigegeben sein (`ADMIN_CIDR` bzw. Hetzner-Firewall), z. B. über VPN.

Voraussetzung auf dem Server: Datenbank läuft (`docker compose up -d db`), Image ist aktuell gebaut.

## Entwicklung

```bash
pip install -e ".[dev]"
pytest                                   # Unit-Tests
GKB_TEST_DATABASE_URL=postgresql://gkb:gkb@localhost:5432/gkb pytest   # inkl. DB-Integrationstest
```

## Nächste Bausteine

2b. Duplikate Stufe 3 und 4 (Fast-Duplikate, semantische Dubletten)
4. Messen: Vergleichsvarianten (klassisches RAG, alle Schilder im Kontext, Stufen-Navigation) über das Evaluationsset
