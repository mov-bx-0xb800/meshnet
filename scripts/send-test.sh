#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT_DIR}/.venv/bin/python"
CONFIG="${1:-config.master.yaml}"
TEXT="${2:-hello from meshnet}"

if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="python3"
fi

cd "${ROOT_DIR}"
"${PYTHON}" -m src.cli send --config "${CONFIG}" --text "${TEXT}"
