#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT_DIR}/.venv/bin/python"
ROLE="${1:-}"

if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3)"
fi

cd "${ROOT_DIR}"

LATEST="$("${PY}" - "${ROLE}" <<'PY'
import sys
from pathlib import Path

role = sys.argv[1].strip()
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
)"

echo "[logs] latest=${LATEST}"

git pull --rebase --autostash
git add -f "${LATEST}"

if git diff --cached --quiet; then
  echo "[logs] nothing to commit"
  exit 0
fi

git commit -m "Add Flower run logs $(basename "${LATEST}")"
git push
