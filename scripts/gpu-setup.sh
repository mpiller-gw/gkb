#!/usr/bin/env bash
# GKB – NVIDIA-Treiber und NVIDIA Container Toolkit für Docker (Ubuntu LTS, z. B. Hetzner GEX44).
#
# Aufruf als root (einmalig, danach Neustart):
#   bash scripts/gpu-setup.sh
# Nach dem Neustart prüfen:
#   nvidia-smi
#   docker run --rm --gpus all ubuntu nvidia-smi
set -euo pipefail

die() { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "Bitte als root ausführen."
command -v docker >/dev/null || die "Docker fehlt – zuerst scripts/server-setup.sh ausführen."
lspci | grep -qi nvidia || die "Keine NVIDIA-GPU gefunden."

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get -y -q install ubuntu-drivers-common pciutils curl gnupg

echo "==> NVIDIA-Treiber (Server-Variante, von Ubuntu empfohlen)"
if ! command -v nvidia-smi >/dev/null || ! nvidia-smi >/dev/null 2>&1; then
  ubuntu-drivers install --gpgpu
  NEUSTART=1
fi

echo "==> NVIDIA Container Toolkit"
if ! command -v nvidia-ctk >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    >/etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -q
  apt-get -y -q install nvidia-container-toolkit
fi
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

if [[ "${NEUSTART:-0}" == "1" ]]; then
  echo
  echo "Treiber installiert. Bitte jetzt neu starten:  reboot"
  echo "Danach das verschlüsselte Volume entsperren (scripts/volume-verschluesseln.sh --entsperren <gerät>)."
else
  nvidia-smi
  docker run --rm --gpus all ubuntu nvidia-smi >/dev/null && echo "Docker sieht die GPU."
fi
