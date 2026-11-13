#!/usr/bin/env bash
# GKB – MCP-Server über SSH für Claude starten (stdio, kein offener Port).
#
# Auf dem Server in ~gkb/.ssh/authorized_keys je Person EINE Zeile mit erzwungenem Befehl:
#   command="/opt/gkb/scripts/mcp.sh michael",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... michael
# Der Name nach mcp.sh ist der Nextcloud-Nutzername und bestimmt die Rechte. Wer diesen Schlüssel
# besitzt, kann damit nur den MCP-Server starten – keine Shell, keine Weiterleitungen.
#
# Für Administratoren, die sich weiterhin normal anmelden: eigener Schlüssel ohne command=.
set -euo pipefail

NUTZER="${1:-}"
[[ "$NUTZER" =~ ^[A-Za-z0-9._@-]+$ ]] || { echo "mcp.sh: ungültiger oder fehlender Nutzername" >&2; exit 1; }

cd "$(dirname "$0")/.."
# Fortschrittsmeldungen von Compose dürfen stdout nicht stören (stdout = MCP-Protokoll)
export COMPOSE_PROGRESS=quiet
# Der MCP-Server braucht keine GPU (ein Embedding je Frage auf der CPU); CUDA_VISIBLE_DEVICES leer hält
# ihn auch dann von der Karte fern, wenn docker-compose.gpu.yml aktiv ist.
exec docker compose run --rm -T --no-deps -e CUDA_VISIBLE_DEVICES= app mcp --nutzer "$NUTZER" \
  2>>"${GKB_MCP_LOG:-/tmp/gkb-mcp.log}"
