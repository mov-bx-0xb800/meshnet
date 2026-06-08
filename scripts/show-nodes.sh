#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT_DIR}/.venv/bin/python"
MESHTASTIC="${ROOT_DIR}/.venv/bin/meshtastic"
PORT="${PORT:-}"

if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="python3"
fi

if [[ ! -x "${MESHTASTIC}" ]]; then
  MESHTASTIC="meshtastic"
fi

if [[ -z "${PORT}" ]]; then
  PORT="$("${PYTHON}" -m src.cli detect --plain || true)"
fi

if [[ -z "${PORT}" ]]; then
  echo "[radio] No serial radio found."
  exit 1
fi

echo "[radio] Using ${PORT}"
"${MESHTASTIC}" --port "${PORT}" --nodes
