#!/usr/bin/env bash
# GKB – tägliches Backup: Datenbank-Dump + Originale, verschlüsselt mit restic (z. B. Hetzner Storage Box).
# Läuft über den systemd-Timer gkb-backup.timer (eingerichtet von server-setup.sh).
# Manuell:  sudo bash -c 'set -a; . /etc/gkb/backup.env; bash scripts/backup.sh'
#
# NICHT im Backup: .env mit den Tresor-Schlüsseln und Zugangsdaten. Diese getrennt sichern.
set -euo pipefail

: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY in /etc/gkb/backup.env setzen}"
: "${RESTIC_PASSWORD_FILE:?}"
REPO_DIR="${GKB_REPO_DIR:-/opt/gkb}"
export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE

cd "$REPO_DIR"
DATA_DIR="$(grep -E '^GKB_DATA_DIR=' .env | cut -d= -f2- || true)"
DATA_DIR="${DATA_DIR:-$REPO_DIR/data}"
mountpoint -q "$DATA_DIR" 2>/dev/null || [[ "$DATA_DIR" == "$REPO_DIR/data" ]] \
  || { echo "Datenverzeichnis $DATA_DIR ist nicht eingehängt (Volume gesperrt?) – kein Backup." >&2; exit 1; }

DUMP_DIR="$DATA_DIR/backup"
mkdir -p "$DUMP_DIR"
STAMP="$(date +%F)"

# 1. Konsistenter Dump der Datenbank (läuft im db-Container, kein Port nach außen nötig)
docker compose exec -T db pg_dump -U gkb -d gkb --format=custom >"$DUMP_DIR/gkb-$STAMP.dump.tmp"
mv "$DUMP_DIR/gkb-$STAMP.dump.tmp" "$DUMP_DIR/gkb-$STAMP.dump"
find "$DUMP_DIR" -name 'gkb-*.dump' -mtime +3 -delete    # lokal nur die letzten Tage

# 2. Dump + Originale verschlüsselt sichern (das laufende Postgres-Verzeichnis selbst nicht)
restic backup --tag gkb --exclude "$DATA_DIR/postgres" "$DUMP_DIR" "$DATA_DIR/originale"

# 3. Aufbewahrung: 7 Tage, 4 Wochen, 6 Monate
restic forget --tag gkb --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune

# 4. Stichprobe der Integrität (5 % der Daten)
restic check --read-data-subset=5%

echo "Backup $STAMP erfolgreich."
