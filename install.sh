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
"${VENV_DIR}/bin/python" -m pip install -e "${ROOT_DIR}"

chmod +x "${ROOT_DIR}"/scripts/*.sh "${ROOT_DIR}/meshnet"

USER_BIN="${HOME}/.local/bin"
LAUNCHER_PATH="${USER_BIN}/meshnet"
mkdir -p "${USER_BIN}"

if [[ -e "${LAUNCHER_PATH}" && "$(readlink "${LAUNCHER_PATH}" 2>/dev/null || true)" != "${ROOT_DIR}/meshnet" ]]; then
  echo "[install] ${LAUNCHER_PATH} already exists; leaving it unchanged."
  echo "[install] You can still run ${ROOT_DIR}/meshnet directly."
else
  ln -sf "${ROOT_DIR}/meshnet" "${LAUNCHER_PATH}"
  echo "[install] Added launcher: ${LAUNCHER_PATH}"
fi

echo "[install] Installation complete."
echo
echo "[next] For the short operator guide:"
echo "       meshnet how-to"
echo
echo "[next] Confirm this Pi has a reachable Meshtastic USB radio:"
echo "       meshnet check master"
echo
echo "[next] Run this on master:"
echo "       meshnet setup master"
echo
echo "[next] Run this on slave:"
echo "       meshnet setup slave"
echo
echo "[next] Then start one runtime per Pi:"
echo "       meshnet slave"
echo "       meshnet master"
echo
echo "[next] For Flower over LoRa, read FLOWER_BRIDGE.md and run only:"
echo "       meshnet bridge --config config.flower.yaml"
echo
echo "[next] If serial permissions were changed, log out and back in before using the radio."

case ":${PATH}:" in
  *":${USER_BIN}:"*) ;;
  *)
    echo "[next] ${USER_BIN} is not in PATH in this shell."
    echo "       Until it is, run: ${ROOT_DIR}/meshnet check master"
    ;;
esac
