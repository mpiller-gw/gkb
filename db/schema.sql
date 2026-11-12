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
