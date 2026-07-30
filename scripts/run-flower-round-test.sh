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

read -r LISTEN_HOST LISTEN_PORT UPSTREAM_HOST UPSTREAM_PORT < <("${PY}" - "${CONFIG}" <<'PY'
import sys, yaml
with open(sys.argv[1], encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh)
bridge = cfg["bridge"]
print(
    bridge["listen_host"],
    bridge["listen_port"],
    bridge["upstream_host"],
    bridge["upstream_port"],
)
PY
)

"${PY}" - "${CONFIG}" <<'PY'
import sys, yaml

with open(sys.argv[1], encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh)

errors = []
radio = cfg.get("radio", {})
telegram = cfg.get("telegram", {})
network = cfg.get("network", {})
bridge = cfg.get("bridge", {})
role = cfg.get("app", {}).get("role")
loopback_hosts = {"127.0.0.1", "localhost", "::1"}
placeholder_mesh_ids = {
    "",
    "!00000001",
    "!00000002",
    "!00000003",
    "!CLIENT_RADIO_ID",
    "!CENTRAL_RADIO_ID",
}

if role not in {"master", "slave"}:
    errors.append("app.role must be master or slave")
if bridge.get("enabled") is not True:
    errors.append("bridge.enabled must be true")
if bridge.get("listen_host") not in loopback_hosts:
    errors.append("bridge.listen_host must be loopback only, usually 127.0.0.1")
if bridge.get("upstream_host") not in loopback_hosts:
    errors.append("bridge.upstream_host must be loopback only, usually 127.0.0.1")
if radio.get("ignore_mqtt") is not True:
    errors.append("radio.ignore_mqtt must be true")
if radio.get("ok_to_mqtt") is not False:
    errors.append("radio.ok_to_mqtt must be false")
if network.get("allow_broadcast") is not False:
    errors.append("network.allow_broadcast must be false")
if telegram.get("enabled") is not False:
    errors.append("telegram.enabled must be false")
if telegram.get("bot_token") or telegram.get("allowed_chat_id"):
    errors.append("telegram bot_token and allowed_chat_id must be empty for this test")

peers = network.get("peers") or []
for index, peer in enumerate(peers):
    mesh_id = str(peer.get("mesh_id", "")).strip()
    if mesh_id in placeholder_mesh_ids:
        errors.append(f"network.peers[{index}].mesh_id is still a placeholder")

if errors:
    print("[run] REFUSING: Flower round test is not locked to local-radio LoRa-only config.", file=sys.stderr)
    for error in errors:
        print(f"[run]   - {error}", file=sys.stderr)
    sys.exit(1)

print("[run] LoRa-only guard passed: MQTT off, Telegram off, loopback TCP only, pinned peer IDs present.")
PY

METRICS_FILE="$("${PY}" - "${CONFIG}" <<'PY'
import sys
from pathlib import Path

print(str(Path(sys.argv[1]).expanduser().resolve().with_suffix(".bridge-metrics.json")))
PY
)"
ROUNDS="${ROUNDS:-1}"
LOGICAL_CLIENTS="${LOGICAL_CLIENTS:-1}"
EVALUATE="${EVALUATE:-1}"
MODEL_BYTES="${MODEL_BYTES:-}"
WEIGHTS_NPZ="${WEIGHTS_NPZ:-}"
STATUS_INTERVAL="${STATUS_INTERVAL:-10}"
BRIDGE_METRICS_INTERVAL="${STATUS_INTERVAL}"
if [[ "${BRIDGE_METRICS_INTERVAL}" == "0" || "${BRIDGE_METRICS_INTERVAL}" == "false" ]]; then
  BRIDGE_METRICS_INTERVAL=60
fi
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
BRIDGE_LOG="flower-bridge-${ROLE}-${RUN_ID}.log"
BENCH_JSONL="flower-round-${ROLE}-${RUN_ID}.jsonl"

stop_services() {
  sudo systemctl stop meshnet-flower-bridge 2>/dev/null || true
  sudo systemctl stop meshnet 2>/dev/null || true
  sudo systemctl stop meshnet-telegram 2>/dev/null || true
}

cleanup() {
  if [[ -n "${STATUS_PID:-}" ]] && kill -0 "${STATUS_PID}" 2>/dev/null; then
    kill "${STATUS_PID}" 2>/dev/null || true
    wait "${STATUS_PID}" 2>/dev/null || true
  fi
  if [[ -n "${BRIDGE_PID:-}" ]] && kill -0 "${BRIDGE_PID}" 2>/dev/null; then
    kill "${BRIDGE_PID}" 2>/dev/null || true
    wait "${BRIDGE_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

start_status_log() {
  if [[ "${STATUS_INTERVAL}" == "0" || "${STATUS_INTERVAL}" == "false" ]]; then
    return
  fi
  echo "[run] concise status every ${STATUS_INTERVAL}s; set STATUS_INTERVAL=0 to hide it"
  "${PY}" - "${METRICS_FILE}" "${STATUS_INTERVAL}" <<'PY' &
import json
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
interval = float(sys.argv[2])
previous = {}

def number(metrics, key):
    return int(metrics.get(key, 0) or 0)

while True:
    time.sleep(interval)
    if not path.exists():
        print("[status] waiting_for_bridge_metrics", flush=True)
        continue
    try:
        metrics = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[status] metrics_read_failed={exc}", flush=True)
        continue

    def delta(key):
        return number(metrics, key) - number(previous, key)

    uptime = metrics.get("uptime_seconds", 0)
    print(
        "[status] "
        f"up={uptime}s "
        f"packets_tx={number(metrics, 'frames_sent')}(+{delta('frames_sent')}) "
        f"packets_rx={number(metrics, 'frames_received')}(+{delta('frames_received')}) "
        f"data_tx={number(metrics, 'data_bytes_sent')}B(+{delta('data_bytes_sent')}) "
        f"data_rx={number(metrics, 'data_bytes_received')}B(+{delta('data_bytes_received')}) "
        f"ack_tx={number(metrics, 'acknowledgements_sent')}(+{delta('acknowledgements_sent')}) "
        f"ack_rx={number(metrics, 'acknowledgements_received')}(+{delta('acknowledgements_received')}) "
        f"retrans={number(metrics, 'retransmitted_frames')}(+{delta('retransmitted_frames')}) "
        f"invalid={number(metrics, 'invalid_frames')}(+{delta('invalid_frames')}) "
        f"opened={number(metrics, 'sessions_opened')} "
        f"resets={number(metrics, 'sessions_reset')}",
        flush=True,
    )
    previous = metrics
PY
  STATUS_PID="$!"
}

echo "[run] role=${ROLE} config=${CONFIG}"
echo "[run] rounds=${ROUNDS} logical_clients=${LOGICAL_CLIENTS} evaluate=${EVALUATE}"
echo "[run] status_interval=${STATUS_INTERVAL}"
if [[ -n "${WEIGHTS_NPZ}" ]]; then
  echo "[run] weights_npz=${WEIGHTS_NPZ}"
elif [[ -n "${MODEL_BYTES}" ]]; then
  echo "[run] model_bytes=${MODEL_BYTES}"
else
  echo "[run] model_bytes=benchmark-default"
fi
echo "[run] bridge_log=${BRIDGE_LOG}"
echo "[run] jsonl=${BENCH_JSONL}"
echo "[run] metrics_file=${METRICS_FILE}"

stop_services
rm -f "${METRICS_FILE}"

echo "[run] starting bridge"
MESHNET_BRIDGE_METRICS_INTERVAL_SECONDS="${BRIDGE_METRICS_INTERVAL}" \
  "${MESHNET}" bridge --config "${CONFIG}" >"${BRIDGE_LOG}" 2>&1 &
BRIDGE_PID="$!"
sleep 5

if ! kill -0 "${BRIDGE_PID}" 2>/dev/null; then
  echo "[run] bridge exited early. Last bridge log lines:" >&2
  tail -n 80 "${BRIDGE_LOG}" >&2 || true
  exit 1
fi

start_status_log

run_benchmark() {
  local status=0
  "$@" || status=$?
  if [[ "${status}" -ne 0 ]]; then
    echo "[run] benchmark failed with exit ${status}. Last bridge log lines:" >&2
    tail -n 120 "${BRIDGE_LOG}" >&2 || true
    exit "${status}"
  fi
}

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
  run_benchmark "${PY}" scripts/flower_round_benchmark.py server \
    --host "${UPSTREAM_HOST}" \
    --port "${UPSTREAM_PORT}" \
    --rounds "${ROUNDS}" \
    --logical-clients "${LOGICAL_CLIENTS}" \
    --jsonl "${BENCH_JSONL}" \
    "${BENCH_ARGS[@]}"
elif [[ "${ROLE}" == "slave" ]]; then
  echo "[run] client benchmark connecting through bridge"
  run_benchmark "${PY}" scripts/flower_round_benchmark.py client \
    --host "${LISTEN_HOST}" \
    --port "${LISTEN_PORT}" \
    --jsonl "${BENCH_JSONL}"
else
  echo "[run] unsupported app.role=${ROLE}; expected master or slave" >&2
  exit 1
fi

echo "[run] done"
