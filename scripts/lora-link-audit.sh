#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-${FLOWER_CONFIG:-config.flower.yaml}}"

ACTIVE_SERVICES=()
SERVICES=(meshnet-flower-bridge meshnet meshnet-telegram)

restore_services() {
  local service
  for service in "${ACTIVE_SERVICES[@]}"; do
    if ! sudo systemctl start "${service}" 2>/dev/null; then
      echo "[audit] warning: could not restore ${service}" >&2
    fi
  done
}

trap restore_services EXIT

if command -v systemctl >/dev/null 2>&1; then
  for service in "${SERVICES[@]}"; do
    if systemctl is-active --quiet "${service}" 2>/dev/null; then
      ACTIVE_SERVICES+=("${service}")
    fi
    sudo systemctl stop "${service}" 2>/dev/null || true
  done
fi

"${ROOT_DIR}/.venv/bin/python" "${ROOT_DIR}/scripts/lora_link_audit.py" "${CONFIG}"
