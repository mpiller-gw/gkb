#!/usr/bin/env bash
# GKB – Hetzner Volume mit LUKS verschlüsseln und als Datenverzeichnis einhängen.
#
# ACHTUNG: Formatiert das angegebene Gerät. Alle Daten darauf gehen verloren.
#
# Aufruf als root:
#   bash scripts/volume-verschluesseln.sh /dev/disk/by-id/scsi-0HC_Volume_12345678
#
# Die Passphrase wird interaktiv abgefragt und NICHT auf dem Server gespeichert.
# Nach jedem Neustart muss das Volume daher einmal entsperrt werden:
#   sudo bash scripts/volume-verschluesseln.sh --entsperren <gerät>
# Das ist gewollt: Ein gestohlenes Image oder Backup des Servers enthält keine lesbaren Daten.
set -euo pipefail

NAME="gkb-data"
MOUNT="${GKB_DATA_DIR:-/srv/gkb-data}"

die() { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Bitte als root ausführen."

entsperren() {
  local dev="$1"
  [[ -e /dev/mapper/$NAME ]] || cryptsetup open "$dev" "$NAME"
  mkdir -p "$MOUNT"
  mountpoint -q "$MOUNT" || mount /dev/mapper/$NAME "$MOUNT"
  echo "Entsperrt und eingehängt unter $MOUNT."
  echo "Danach GKB starten:  cd /opt/gkb && docker compose up -d db"
}

if [[ "${1:-}" == "--entsperren" ]]; then
  [[ -n "${2:-}" ]] || die "Gerät angeben."
  entsperren "$2"
  exit 0
fi

DEV="${1:-}"
[[ -n "$DEV" && -b "$DEV" ]] || die "Blockgerät angeben, z. B. /dev/disk/by-id/scsi-0HC_Volume_… (lsblk zeigt die Geräte)."
if grep -q "$(readlink -f "$DEV")" /proc/mounts; then
  die "$DEV ist eingehängt. Bei Hetzner Volumes vorher die automatische Einbindung in /etc/fstab entfernen und aushängen."
fi
if cryptsetup isLuks "$DEV" 2>/dev/null; then
  echo "$DEV ist bereits LUKS-verschlüsselt – es wird nur entsperrt."
  entsperren "$DEV"
  exit 0
fi

lsblk -o NAME,SIZE,TYPE,MOUNTPOINT "$DEV"
read -r -p "ALLE DATEN auf $DEV werden gelöscht. Zum Fortfahren 'JA' eingeben: " ok
[[ "$ok" == "JA" ]] || die "Abgebrochen."

cryptsetup luksFormat --type luks2 "$DEV"
cryptsetup open "$DEV" "$NAME"
mkfs.ext4 -L gkb-data /dev/mapper/$NAME
mkdir -p "$MOUNT"
mount /dev/mapper/$NAME "$MOUNT"
install -d -m 750 "$MOUNT/postgres" "$MOUNT/originale" "$MOUNT/backup"
chown 999:999 "$MOUNT/postgres"   # Benutzer "postgres" im pgvector-Container
chown 1000:1000 "$MOUNT/originale" 2>/dev/null || true

echo
echo "Fertig: verschlüsselt und eingehängt unter $MOUNT."
echo "Passphrase sicher außerhalb des Servers aufbewahren. In .env: GKB_DATA_DIR=$MOUNT"
