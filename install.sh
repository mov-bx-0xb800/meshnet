#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"

echo "[install] Installing MeshNet..."
echo "[install] Checking OS..."

if command -v apt-get >/dev/null 2>&1; then
  echo "[install] Installing dependencies..."
  sudo apt-get update
  sudo apt-get install -y python3 python3-pip python3-venv
else
  echo "[install] apt-get not found; install python3, python3-pip, and python3-venv manually."
fi

echo "[install] Adding user to dialout group..."
if getent group dialout >/dev/null 2>&1; then
  sudo usermod -a -G dialout "${USER}"
  echo "[install] User ${USER} added to dialout. Log out and back in for this to take effect."
else
  echo "[install] No dialout group found on this OS; skipping."
fi

echo "[install] Creating environment..."
python3 -m venv "${VENV_DIR}"

echo "[install] Installing project packages..."
"${VENV_DIR}/bin/python" -m pip install --upgrade pip wheel
"${VENV_DIR}/bin/python" -m pip install -r "${ROOT_DIR}/requirements.txt"

chmod +x "${ROOT_DIR}"/scripts/*.sh

echo "[install] Installation complete."
echo
echo "[next] Confirm this Pi has a reachable Meshtastic USB radio:"
echo "       ${VENV_DIR}/bin/python -m src.cli preflight --config config.master.yaml"
echo
echo "[next] Run this on master:"
echo "       ${VENV_DIR}/bin/python -m src.cli setup-radio --config config.master.yaml"
echo
echo "[next] Run this on slave:"
echo "       ${VENV_DIR}/bin/python -m src.cli setup-radio --config config.slave.yaml"
echo
echo "[next] Then run:"
echo "       ${VENV_DIR}/bin/python -m src.cli test --config config.master.yaml"
echo
echo "[next] If serial permissions were changed, log out and back in before using the radio."
