#!/usr/bin/env bash
# GKB – Ersteinrichtung eines Hetzner-Servers (Ubuntu 22.04/24.04 LTS).
#
# Aufruf als root, direkt nach dem Anlegen des Servers (SSH-Schlüssel bei Hetzner hinterlegt):
#   ADMIN_CIDR=203.0.113.10/32 bash scripts/server-setup.sh
#
# Was das Skript tut (idempotent, mehrfach ausführbar):
#   1. System aktualisieren, automatische Sicherheitsupdates
#   2. Benutzer "gkb" mit den SSH-Schlüsseln von root anlegen
#   3. SSH härten: nur Schlüssel, kein root-Login (erst nachdem der Schlüssel für "gkb" geprüft ist)
#   4. Firewall (ufw): SSH nur aus ADMIN_CIDR; fail2ban
#   5. Docker Engine + Compose-Plugin aus dem offiziellen Docker-Repository
#   6. Backup-Werkzeug restic und den täglichen Backup-Timer (scripts/backup.sh)
#
# Nicht enthalten (bewusst getrennt, weil zerstörerisch bzw. interaktiv):
#   - Verschlüsseltes Datenvolume: scripts/volume-verschluesseln.sh
#   - Hetzner-Firewall in der Cloud Console (zusätzlich zu ufw empfohlen)
set -euo pipefail

GKB_USER="${GKB_USER:-gkb}"
ADMIN_CIDR="${ADMIN_CIDR:-}"
REPO_DIR="${REPO_DIR:-/opt/gkb}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Bitte als root ausführen."
[[ -n "$ADMIN_CIDR" ]] || die "ADMIN_CIDR setzen (eure feste IP oder VPN-Netz, z. B. 203.0.113.10/32)."
. /etc/os-release
[[ "$ID" == "ubuntu" ]] || die "Getestet für Ubuntu LTS, gefunden: $PRETTY_NAME"

log "1/6 System aktualisieren"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get -y -q upgrade
apt-get -y -q install ca-certificates curl gnupg ufw fail2ban unattended-upgrades restic cryptsetup
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

log "2/6 Benutzer $GKB_USER"
if ! id "$GKB_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "GKB" "$GKB_USER"
fi
usermod -aG sudo "$GKB_USER"
install -d -m 700 -o "$GKB_USER" -g "$GKB_USER" "/home/$GKB_USER/.ssh"
if [[ -s /root/.ssh/authorized_keys ]]; then
  touch "/home/$GKB_USER/.ssh/authorized_keys"
  # Schlüssel von root übernehmen, ohne vorhandene zu verdoppeln
  sort -u /root/.ssh/authorized_keys "/home/$GKB_USER/.ssh/authorized_keys" -o "/home/$GKB_USER/.ssh/authorized_keys"
  chown "$GKB_USER:$GKB_USER" "/home/$GKB_USER/.ssh/authorized_keys"
  chmod 600 "/home/$GKB_USER/.ssh/authorized_keys"
fi
[[ -s "/home/$GKB_USER/.ssh/authorized_keys" ]] || die "Kein SSH-Schlüssel für $GKB_USER – Abbruch, damit ihr euch nicht aussperrt."
# sudo ohne Passwort wäre bequem, ist aber unnötig riskant: Passwort für sudo setzen
if passwd -S "$GKB_USER" | awk '{print $2}' | grep -q '^L'; then
  echo "Bitte jetzt ein sudo-Passwort für $GKB_USER setzen:"
  passwd "$GKB_USER"
fi

log "3/6 SSH härten"
cat >/etc/ssh/sshd_config.d/99-gkb.conf <<EOF
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers $GKB_USER
EOF
sshd -t || die "SSH-Konfiguration ungültig"
systemctl reload ssh 2>/dev/null || systemctl reload sshd
echo "Root-Login ist ab jetzt gesperrt. Neue Sitzungen: ssh $GKB_USER@<server>"

log "4/6 Firewall und fail2ban"
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$ADMIN_CIDR" to any port 22 proto tcp comment "SSH Admin"
ufw --force enable
systemctl enable --now fail2ban

log "5/6 Docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get -y -q install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
usermod -aG docker "$GKB_USER"
# Docker umgeht ufw bei veröffentlichten Ports. GKB veröffentlicht keine Ports – das soll so bleiben.

log "6/6 Backup-Timer"
install -d -m 750 /etc/gkb
if [[ ! -f /etc/gkb/backup.env ]]; then
  cat >/etc/gkb/backup.env <<'EOF'
# restic-Ziel, z. B. Hetzner Storage Box per SFTP:
#   RESTIC_REPOSITORY=sftp:u123456@u123456.your-storagebox.de:/gkb
RESTIC_REPOSITORY=
# Datei mit dem restic-Passwort (zufällig erzeugt, zusätzlich AUSSERHALB des Servers aufbewahren!)
RESTIC_PASSWORD_FILE=/etc/gkb/restic-passwort
# Projektordner mit docker-compose.yml und .env
GKB_REPO_DIR=/opt/gkb
EOF
  chmod 600 /etc/gkb/backup.env
fi
if [[ ! -f /etc/gkb/restic-passwort ]]; then
  head -c 32 /dev/urandom | base64 >/etc/gkb/restic-passwort
  chmod 600 /etc/gkb/restic-passwort
fi
cat >/etc/systemd/system/gkb-backup.service <<EOF
[Unit]
Description=GKB Backup (Datenbank-Dump + Originale)
After=docker.service

[Service]
Type=oneshot
EnvironmentFile=/etc/gkb/backup.env
ExecStart=/bin/bash ${REPO_DIR}/scripts/backup.sh
EOF
cat >/etc/systemd/system/gkb-backup.timer <<'EOF'
[Unit]
Description=GKB Backup täglich

[Timer]
OnCalendar=*-*-* 02:30:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable gkb-backup.timer

install -d -o "$GKB_USER" -g "$GKB_USER" "$REPO_DIR"

cat <<EOF

Fertig. Nächste Schritte:
  1. Hetzner Cloud Console: Firewall anlegen, eingehend nur TCP 22 aus $ADMIN_CIDR.
  2. Verschlüsseltes Datenvolume einrichten:   sudo bash scripts/volume-verschluesseln.sh
  3. Als $GKB_USER: Repository nach $REPO_DIR klonen, .env aus .env.example anlegen (chmod 600).
  4. /etc/gkb/backup.env ausfüllen, restic-Repository anlegen:
       sudo bash -c 'set -a; . /etc/gkb/backup.env; restic init'
  5. restic-Passwort (/etc/gkb/restic-passwort) und die Tresor-Schlüssel aus .env
     (GKB_VAULT_KEY, GKB_VAULT_HMAC_KEY) getrennt und sicher außerhalb des Servers ablegen.
     Ohne sie sind Backups bzw. Pseudonyme nicht wiederherstellbar.
EOF
