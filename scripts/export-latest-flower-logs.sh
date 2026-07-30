#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT_DIR}/.venv/bin/python"
ROLE="${1:-}"
RUN_DIR_ARG="${2:-}"
EXPORT_DIR="${EXPORT_DIR:-${ROOT_DIR}/exports}"
SERVE="${SERVE:-0}"
PORT="${PORT:-8765}"
SERVE_BIND="${SERVE_BIND:-127.0.0.1}"

if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3)"
fi

cd "${ROOT_DIR}"

bool_enabled() {
  local value="${1:-0}"
  [[ "${value}" != "0" && "${value}" != "false" && "${value}" != "no" ]]
}

resolve_run_dir() {
  "${PY}" - "${ROLE}" "${RUN_DIR_ARG}" <<'PY'
import sys
from pathlib import Path

role = sys.argv[1].strip()
arg = sys.argv[2].strip()
if arg:
    path = Path(arg)
    if not path.exists() or not path.is_dir():
        raise SystemExit(f"log directory does not exist: {path}")
    print(path)
    raise SystemExit(0)

root = Path("logs")
roles = []
if role:
    role = {"master": "central", "slave": "client"}.get(role, role)
    roles = [role]
else:
    roles = ["central", "client"]

dirs = []
for item in roles:
    base = root / item
    if base.exists():
        dirs.extend(path for path in base.iterdir() if path.is_dir())

if not dirs:
    raise SystemExit(f"no log directories found for {','.join(roles)}")

latest = max(dirs, key=lambda path: path.stat().st_mtime)
print(latest)
PY
}

path_info() {
  "${PY}" - "${ROOT_DIR}" "$1" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
path = Path(sys.argv[2])
if not path.is_absolute():
    path = root / path
path = path.resolve()
try:
    rel = path.relative_to(root)
except ValueError as exc:
    raise SystemExit(f"log directory is outside repo: {path}") from exc
print(f"{path}\t{rel.as_posix()}")
PY
}

print_urls() {
  local archive_name="$1"
  local hostnames=()
  if [[ "${SERVE_BIND}" != "0.0.0.0" && "${SERVE_BIND}" != "::" ]]; then
    hostnames=("${SERVE_BIND}")
  fi
  if [[ "${#hostnames[@]}" -eq 0 ]] && command -v hostname >/dev/null 2>&1; then
    while IFS= read -r ip; do
      [[ -n "${ip}" ]] && hostnames+=("${ip}")
    done < <(hostname -I 2>/dev/null | tr ' ' '\n' | sed '/^$/d' || true)
  fi
  if [[ "${#hostnames[@]}" -eq 0 ]] && command -v ip >/dev/null 2>&1; then
    while IFS= read -r ip; do
      [[ -n "${ip}" ]] && hostnames+=("${ip}")
    done < <(ip -4 addr show scope global 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' || true)
  fi
  if [[ "${#hostnames[@]}" -eq 0 ]]; then
    hostnames=("127.0.0.1")
  fi
  for ip in "${hostnames[@]}"; do
    echo "[export] url=http://${ip}:${PORT}/${archive_name}"
  done
}

RUN_DIR="$(resolve_run_dir)"
IFS=$'\t' read -r RUN_DIR_ABS RUN_DIR_REL < <(path_info "${RUN_DIR}")
LOG_ROLE="$(basename "$(dirname "${RUN_DIR_REL}")")"
RUN_NAME="$(basename "${RUN_DIR_REL}")"
ARCHIVE_NAME="flower-logs-${LOG_ROLE}-${RUN_NAME}.tar.gz"
ARCHIVE_PATH="${EXPORT_DIR}/${ARCHIVE_NAME}"

mkdir -p "${EXPORT_DIR}"
tar -C "${ROOT_DIR}" -czf "${ARCHIVE_PATH}" "${RUN_DIR_REL}"

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${ARCHIVE_PATH}" > "${ARCHIVE_PATH}.sha256"
elif command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "${ARCHIVE_PATH}" > "${ARCHIVE_PATH}.sha256"
fi

echo "[export] run_dir=${RUN_DIR_REL}"
echo "[export] archive=${ARCHIVE_PATH}"
ls -lh "${ARCHIVE_PATH}" 2>/dev/null || true
if [[ -f "${ARCHIVE_PATH}.sha256" ]]; then
  echo "[export] sha256=${ARCHIVE_PATH}.sha256"
fi
echo "[export] easiest: attach this .tar.gz file here"

if bool_enabled "${SERVE}"; then
  echo "[export] serving ${EXPORT_DIR} on ${SERVE_BIND}:${PORT}; press Ctrl+C when downloaded"
  if [[ "${SERVE_BIND}" == "0.0.0.0" || "${SERVE_BIND}" == "::" ]]; then
    echo "[export] WARNING: archive is unauthenticated and reachable from the network"
  fi
  print_urls "${ARCHIVE_NAME}"
  cd "${EXPORT_DIR}"
  exec "${PY}" -m http.server "${PORT}" --bind "${SERVE_BIND}"
fi
