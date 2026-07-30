#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT_DIR}/.venv/bin/python"
PORT="${1:-}"

if [[ ! -x "${PY}" ]]; then
  echo "Missing venv. Run: cd ${ROOT_DIR} && ./install.sh" >&2
  exit 1
fi

if [[ -z "${PORT}" ]]; then
  PORT="$("${ROOT_DIR}/meshnet" detect --plain)"
fi

"${PY}" - "${PORT}" <<'PY'
import sys

import meshtastic.serial_interface

port = sys.argv[1]
interface = meshtastic.serial_interface.SerialInterface(
    devPath=port,
    noNodes=True,
    timeout=60,
)
try:
    my_info = getattr(interface, "myInfo", None)
    node_num = None
    if my_info is not None:
        node_num = getattr(my_info, "my_node_num", None) or getattr(my_info, "myNodeNum", None)
    if node_num is None:
        node_num = getattr(interface.localNode, "nodeNum", None)
    if node_num is None:
        raise RuntimeError("could not read local Meshtastic node number")
    print(f"!{int(node_num):08x}")
finally:
    interface.close()
PY
