-- GKB Schema, Baustein 1 (Einlesen). Idempotent ausführbar.
CREATE EXTENSION IF NOT EXISTS vector;

-- Eine angebundene Quelle (z. B. Nextcloud-Ordner, Teams, Ingest-API)
CREATE TABLE IF NOT EXISTS quelle (
    id          serial PRIMARY KEY,
    art         text NOT NULL,                 -- nextcloud | teams | api | ...
    name        text NOT NULL UNIQUE,
    konfig      jsonb NOT NULL DEFAULT '{}',
    erstellt    timestamptz NOT NULL DEFAULT now()
);

-- Jede Anlieferung bzw. jeder abgeholte Dateistand = ein Lieferbeleg (Herkunft)
CREATE TABLE IF NOT EXISTS lieferbeleg (
    id           bigserial PRIMARY KEY,
    quelle_id    int NOT NULL REFERENCES quelle(id),
    externe_id   text NOT NULL,                -- Nextcloud: fileid
    version      text NOT NULL,                -- Nextcloud: ETag
    sha256       text,
    pfad         text,
    deep_link    text,
    mime         text,
    groesse      bigint,
    geaendert    timestamptz,
    empfangen    timestamptz NOT NULL DEFAULT now(),
    status       text NOT NULL CHECK (status IN ('neu','update','verschoben','duplikat','konflikt','fehler')),
    ablage_pfad  text,                         -- unverändertes Original auf dem Server
    fehler       text,
    UNIQUE (quelle_id, externe_id, version)    -- Duplikat-Stufe 1: Idempotenz
);
CREATE INDEX IF NOT EXISTS lieferbeleg_sha ON lieferbeleg (sha256);

-- Aktueller Stand eines Dokuments (pseudonymisierter Inhalt als Markdown)
CREATE TABLE IF NOT EXISTS rohdokument (
    id                 bigserial PRIMARY KEY,
    quelle_id          int NOT NULL REFERENCES quelle(id),
    externe_id         text NOT NULL,
    beleg_id           bigint REFERENCES lieferbeleg(id),
    titel              text,
    pfad               text,
    deep_link          text,
    mime               text,
    sha256             text NOT NULL,
    markdown_pseudo    text,                   -- Inhalt nach Pseudonymisierung
    rechte_partition   text NOT NULL,
    geloescht          boolean NOT NULL DEFAULT false,
    aktualisiert       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (quelle_id, externe_id)
);
CREATE INDEX IF NOT EXISTS rohdokument_sha ON rohdokument (sha256);

-- Weitere Herkünfte desselben Inhalts (Duplikat-Stufe 2)
CREATE TABLE IF NOT EXISTS dokument_herkunft (
    rohdokument_id  bigint NOT NULL REFERENCES rohdokument(id),
    lieferbeleg_id  bigint NOT NULL REFERENCES lieferbeleg(id),
    PRIMARY KEY (rohdokument_id, lieferbeleg_id)
);

-- Kunden aus Lexoffice
CREATE TABLE IF NOT EXISTS kunde (
    kundennummer   text PRIMARY KEY,
    name           text NOT NULL,
    ist_person     boolean NOT NULL DEFAULT false,   -- Einzelunternehmer/Privatperson
    domains        text[] NOT NULL DEFAULT '{}',
    aktualisiert   timestamptz NOT NULL DEFAULT now()
);

-- Ansprechpartner u. a. Personen, die pseudonymisiert werden müssen (Klarnamen nur verschlüsselt)
CREATE TABLE IF NOT EXISTS personenliste (
    wert_hash           text PRIMARY KEY,
    wert_verschluesselt bytea NOT NULL,
    typ                 text NOT NULL,       -- PERSON | EMAIL | ...
    herkunft            text NOT NULL
);

-- Zuordnungstresor: Pseudonym <-> Klarwert (verschlüsselt)
CREATE TABLE IF NOT EXISTS pseudonym (
    kuerzel              text PRIMARY KEY,   -- z. B. [PERSON_0042]
    typ                  text NOT NULL,
    wert_hash            text NOT NULL UNIQUE,
    wert_verschluesselt  bytea NOT NULL,
    erstellt             timestamptz NOT NULL DEFAULT now()
);

-- Lineage: welches Artefakt entstand aus welchen Eingaben
CREATE TABLE IF NOT EXISTS lineage (
    abgeleitet_typ  text NOT NULL,
    abgeleitet_id   text NOT NULL,
    eingabe_typ     text NOT NULL,
    eingabe_id      text NOT NULL,
    prozess         text NOT NULL,       -- Pipeline + Version
    erstellt        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (abgeleitet_typ, abgeleitet_id, eingabe_typ, eingabe_id)
);

-- Protokoll je Sync-Lauf
CREATE TABLE IF NOT EXISTS sync_lauf (
    id          bigserial PRIMARY KEY,
    quelle_id   int NOT NULL REFERENCES quelle(id),
    start       timestamptz NOT NULL DEFAULT now(),
    ende        timestamptz,
    zaehler     jsonb NOT NULL DEFAULT '{}'
);

-- ============================================================================
-- Baustein 1b: Rechte-Partitionen
-- ============================================================================
CREATE TABLE IF NOT EXISTS rechte_partition (
    id            text PRIMARY KEY,                -- rp-<hash> bzw. Pfad-Fallback
    erlaubt       text[] NOT NULL,                 -- user:<id> | group:<id> | pfad:<ordner>
    verboten      text[] NOT NULL DEFAULT '{}',
    vollstaendig  boolean NOT NULL DEFAULT true,   -- false: nur Administratoren sehen den Inhalt
    hinweis       text NOT NULL DEFAULT '',
    erstellt      timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================
-- Baustein 2: Aufbereitung
-- ============================================================================
CREATE TABLE IF NOT EXISTS projekt (
    id      text PRIMARY KEY,                      -- abgeleitet aus dem Ordnerpfad
    name    text NOT NULL,
    kunde   text REFERENCES kunde(kundennummer),
    pfad    text NOT NULL UNIQUE
);

-- Abschnitte (Stufe 3 und Grundlage für klassisches RAG)
CREATE TABLE IF NOT EXISTS abschnitt (
    id                bigserial PRIMARY KEY,
    rohdokument_id    bigint NOT NULL REFERENCES rohdokument(id) ON DELETE CASCADE,
    nr                int NOT NULL,
    ueberschrift      text,
    text              text NOT NULL,
    tokens            int NOT NULL,
    embedding         vector(1024),
    tsv               tsvector GENERATED ALWAYS AS (to_tsvector('german', coalesce(ueberschrift,'') || ' ' || text)) STORED,
    rechte_partition  text NOT NULL,
    UNIQUE (rohdokument_id, nr)
);
CREATE INDEX IF NOT EXISTS abschnitt_tsv ON abschnitt USING gin (tsv);
CREATE INDEX IF NOT EXISTS abschnitt_emb ON abschnitt USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS abschnitt_rp ON abschnitt (rechte_partition);

-- Kontrolliertes Vokabular (Stufe 1)
CREATE TABLE IF NOT EXISTS vokabular (
    facette   text NOT NULL,
    wert      text NOT NULL,
    eltern    text,
    status    text NOT NULL DEFAULT 'aktiv' CHECK (status IN ('aktiv','vorschlag','abgelehnt')),
    synonyme  text[] NOT NULL DEFAULT '{}',
    anzahl    int NOT NULL DEFAULT 0,              -- wie oft vorgeschlagen bzw. vergeben
    PRIMARY KEY (facette, wert)
);

-- Klingelschilder (Stufe 2): Einheiten und Cluster
CREATE TABLE IF NOT EXISTS klingelschild (
    id                text PRIMARY KEY,
    typ               text NOT NULL CHECK (typ IN ('einheit','cluster')),
    ebene             text NOT NULL,               -- einheit | projekt | kunde | wissensraum
    schluessel        text NOT NULL,               -- rohdokument-ID bzw. Cluster-Schlüssel
    rohdokument_id    bigint REFERENCES rohdokument(id) ON DELETE CASCADE,
    eltern_id         text,
    daten             jsonb NOT NULL,              -- gespeicherte Form (vollständig)
    text_ansicht      text NOT NULL,               -- kompakte Ansicht für das LLM
    tags              jsonb NOT NULL DEFAULT '{}', -- Filterfelder
    embedding         vector(1024),
    rechte_partition  text NOT NULL,
    eingabe_hash      text NOT NULL,               -- unverändert -> nicht neu erzeugen
    modell            text NOT NULL,
    prompt_version    text NOT NULL,
    aktualisiert      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ebene, schluessel, rechte_partition)
);
CREATE INDEX IF NOT EXISTS klingelschild_emb ON klingelschild USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS klingelschild_tags ON klingelschild USING gin (tags);
CREATE INDEX IF NOT EXISTS klingelschild_rp ON klingelschild (rechte_partition);

-- Faktentabellen (Datenpfad)
CREATE TABLE IF NOT EXISTS angebot (
    rohdokument_id     bigint PRIMARY KEY REFERENCES rohdokument(id) ON DELETE CASCADE,
    gruppe             text NOT NULL,              -- Ordner + Dateiname ohne Endung: verbindet Office- und PDF-Fassung
    fassung            text NOT NULL CHECK (fassung IN ('office','pdf','sonstige')),
    angebotsnummer     text,
    version            text,
    kunde              text,
    projekt            text,
    gesamt_netto       numeric,
    summe_positionen   numeric,
    gueltig_bis        date,
    status             text NOT NULL CHECK (status IN ('ok','pruefen')),
    pruefhinweis       text NOT NULL DEFAULT '',
    rechte_partition   text NOT NULL
);

CREATE TABLE IF NOT EXISTS angebotsposition (
    id                 bigserial PRIMARY KEY,
    rohdokument_id     bigint NOT NULL REFERENCES rohdokument(id) ON DELETE CASCADE,
    position           text,
    leistung           text NOT NULL,
    menge              numeric,
    einheit            text,
    einzelpreis        numeric,
    gesamt             numeric,
    rechte_partition   text NOT NULL
);
CREATE INDEX IF NOT EXISTS angebotsposition_doc ON angebotsposition (rohdokument_id);

-- Sicht für den Datenpfad: nur die maßgebliche Fassung je Angebot (PDF vor Office)
CREATE OR REPLACE VIEW angebot_massgeblich AS
SELECT DISTINCT ON (a.gruppe) a.*, r.deep_link, r.titel
FROM angebot a JOIN rohdokument r ON r.id = a.rohdokument_id
WHERE NOT r.geloescht
ORDER BY a.gruppe, CASE a.fassung WHEN 'pdf' THEN 0 WHEN 'office' THEN 1 ELSE 2 END;
