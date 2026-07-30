#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-${FLOWER_CONFIG:-config.flower.yaml}}"

sudo systemctl stop meshnet-flower-bridge 2>/dev/null || true
sudo systemctl stop meshnet 2>/dev/null || true
sudo systemctl stop meshnet-telegram 2>/dev/null || true

exec "${ROOT_DIR}/.venv/bin/python" "${ROOT_DIR}/scripts/lora_link_audit.py" "${CONFIG}"
