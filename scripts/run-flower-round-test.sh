#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-${FLOWER_CONFIG:-config.flower.yaml}}"
PY="${ROOT_DIR}/.venv/bin/python"
MESHNET="${ROOT_DIR}/meshnet"

if [[ ! -x "${PY}" ]]; then
  echo "[run] Missing venv. Run: cd ${ROOT_DIR} && ./install.sh" >&2
  exit 1
fi

cd "${ROOT_DIR}"

if [[ ! -f "${CONFIG}" ]]; then
  echo "[run] Missing ${CONFIG}." >&2
  echo "[run] Create it from config.flower-central.example.yaml on central or config.flower-client.example.yaml on client, then put your real peer mesh IDs in it." >&2
  exit 1
fi

ROLE="$("${PY}" - "${CONFIG}" <<'PY'
import sys, yaml
with open(sys.argv[1], encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh)
print(cfg["app"]["role"])
PY
)"

read -r BRIDGE_HOST BRIDGE_PORT < <("${PY}" - "${CONFIG}" <<'PY'
import sys, yaml
with open(sys.argv[1], encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh)
bridge = cfg["bridge"]
print(bridge["listen_host"], bridge["listen_port"])
PY
)

ROUNDS="${ROUNDS:-1}"
LOGICAL_CLIENTS="${LOGICAL_CLIENTS:-1}"
EVALUATE="${EVALUATE:-1}"
MODEL_BYTES="${MODEL_BYTES:-}"
WEIGHTS_NPZ="${WEIGHTS_NPZ:-}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
BRIDGE_LOG="flower-bridge-${ROLE}-${RUN_ID}.log"
BENCH_JSONL="flower-round-${ROLE}-${RUN_ID}.jsonl"

stop_services() {
  sudo systemctl stop meshnet-flower-bridge 2>/dev/null || true
  sudo systemctl stop meshnet 2>/dev/null || true
  sudo systemctl stop meshnet-telegram 2>/dev/null || true
}

cleanup() {
  if [[ -n "${BRIDGE_PID:-}" ]] && kill -0 "${BRIDGE_PID}" 2>/dev/null; then
    kill "${BRIDGE_PID}" 2>/dev/null || true
    wait "${BRIDGE_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[run] role=${ROLE} config=${CONFIG}"
echo "[run] rounds=${ROUNDS} logical_clients=${LOGICAL_CLIENTS} evaluate=${EVALUATE}"
if [[ -n "${WEIGHTS_NPZ}" ]]; then
  echo "[run] weights_npz=${WEIGHTS_NPZ}"
elif [[ -n "${MODEL_BYTES}" ]]; then
  echo "[run] model_bytes=${MODEL_BYTES}"
else
  echo "[run] model_bytes=benchmark-default"
fi
echo "[run] bridge_log=${BRIDGE_LOG}"
echo "[run] jsonl=${BENCH_JSONL}"

stop_services

echo "[run] starting bridge"
"${MESHNET}" bridge --config "${CONFIG}" >"${BRIDGE_LOG}" 2>&1 &
BRIDGE_PID="$!"
sleep 5

if ! kill -0 "${BRIDGE_PID}" 2>/dev/null; then
  echo "[run] bridge exited early. Last bridge log lines:" >&2
  tail -n 80 "${BRIDGE_LOG}" >&2 || true
  exit 1
fi

BENCH_ARGS=()
if [[ "${EVALUATE}" == "0" || "${EVALUATE}" == "false" ]]; then
  BENCH_ARGS+=(--no-evaluate)
fi
if [[ -n "${MODEL_BYTES}" ]]; then
  BENCH_ARGS+=(--model-bytes "${MODEL_BYTES}")
fi
if [[ -n "${WEIGHTS_NPZ}" ]]; then
  BENCH_ARGS+=(--weights-npz "${WEIGHTS_NPZ}")
fi

if [[ "${ROLE}" == "master" ]]; then
  echo "[run] central benchmark server waiting for client"
  "${PY}" scripts/flower_round_benchmark.py server \
    --host "${BRIDGE_HOST}" \
    --port "${BRIDGE_PORT}" \
    --rounds "${ROUNDS}" \
    --logical-clients "${LOGICAL_CLIENTS}" \
    --jsonl "${BENCH_JSONL}" \
    "${BENCH_ARGS[@]}"
elif [[ "${ROLE}" == "slave" ]]; then
  echo "[run] client benchmark connecting through bridge"
  "${PY}" scripts/flower_round_benchmark.py client \
    --host "${BRIDGE_HOST}" \
    --port "${BRIDGE_PORT}" \
    --jsonl "${BENCH_JSONL}"
else
  echo "[run] unsupported app.role=${ROLE}; expected master or slave" >&2
  exit 1
fi

echo "[run] done"
