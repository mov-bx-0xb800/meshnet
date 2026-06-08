#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT_DIR}/.venv/bin/python"
CONFIG="${1:-config.master.yaml}"

if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="python3"
fi

cd "${ROOT_DIR}"
"${PYTHON}" -m src.cli preflight --config "${CONFIG}"
