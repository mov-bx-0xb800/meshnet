#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-${FLOWER_CONFIG:-config.flower.yaml}}"
PY="${ROOT_DIR}/.venv/bin/python"
MESHNET="${ROOT_DIR}/meshnet"
MESHTASTIC_CLI="${PY%/*}/meshtastic"
MESHTASTIC_INFO="${ROOT_DIR}/scripts/meshtastic-info-offline.py"

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
import ipaddress
import sys

import yaml
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

case "${ROLE}" in
  master) LOG_ROLE="central" ;;
  slave) LOG_ROLE="client" ;;
  *) LOG_ROLE="unknown" ;;
esac

ROUNDS="${ROUNDS:-3}"
LOGICAL_CLIENTS="${LOGICAL_CLIENTS:-2}"
EVALUATE="${EVALUATE:-1}"
MODEL_BYTES="${MODEL_BYTES:-48712}"
WEIGHTS_NPZ="${WEIGHTS_NPZ:-}"
BRIDGE_PAYLOAD_BYTES="${BRIDGE_PAYLOAD_BYTES:-160}"
BRIDGE_WINDOW_SIZE="${BRIDGE_WINDOW_SIZE:-8}"
BRIDGE_ACK_TIMEOUT_SECONDS="${BRIDGE_ACK_TIMEOUT_SECONDS:-5}"
BRIDGE_CONTROL_TIMEOUT_SECONDS="${BRIDGE_CONTROL_TIMEOUT_SECONDS:-10}"
BRIDGE_MAX_RETRIES="${BRIDGE_MAX_RETRIES:-8}"
BRIDGE_FRAME_INTERVAL_MS="${BRIDGE_FRAME_INTERVAL_MS:-400}"
BRIDGE_POLL_INTERVAL_MS="${BRIDGE_POLL_INTERVAL_MS:-500}"
# Firmware and Git revisions are useful diagnostics, but a mismatch must not
# make the central runner look like it crashed as soon as the client connects.
# Set this to 0 only for a deliberately strict acceptance run.
ALLOW_PEER_MISMATCH="${ALLOW_PEER_MISMATCH:-1}"
STATUS_INTERVAL="${STATUS_INTERVAL:-10}"
CAPTURE_RADIO_INFO="${CAPTURE_RADIO_INFO:-1}"
EXPORT_LOG_ARCHIVE="${EXPORT_LOG_ARCHIVE:-1}"
EXPORT_DIR="${EXPORT_DIR:-${ROOT_DIR}/exports}"
JOURNAL_SINCE="${JOURNAL_SINCE:-2 hours ago}"
BRIDGE_METRICS_INTERVAL="${STATUS_INTERVAL}"
if [[ "${BRIDGE_METRICS_INTERVAL}" == "0" || "${BRIDGE_METRICS_INTERVAL}" == "false" ]]; then
  BRIDGE_METRICS_INTERVAL=60
fi

HOST_SHORT="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo node)"
HOST_SHORT="${HOST_SHORT//[^A-Za-z0-9_.-]/_}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-${HOST_SHORT}-$$"
RUN_DIR="${ROOT_DIR}/logs/${LOG_ROLE}/${RUN_ID}"
RUN_LOG="${RUN_DIR}/runner.log"
STATUS_LOG="${RUN_DIR}/status.log"
STATUS_JSONL="${RUN_DIR}/status.jsonl"
BRIDGE_LOG="${RUN_DIR}/bridge.log"
BENCH_LOG="${RUN_DIR}/benchmark.log"
BENCH_JSONL="${RUN_DIR}/benchmark.jsonl"
RADIO_INFO_LOG="${RUN_DIR}/radio-info.log"
SYSTEMD_BEFORE_LOG="${RUN_DIR}/systemd-before.log"
SYSTEMD_AFTER_LOG="${RUN_DIR}/systemd-after.log"
SERVICES_BEFORE_FILE="${RUN_DIR}/services-before.txt"
METADATA_FILE="${RUN_DIR}/metadata.txt"
REDACTED_CONFIG="${RUN_DIR}/config.redacted.yaml"
METRICS_FINAL="${RUN_DIR}/metrics-final.json"
ALL_LOG="${RUN_DIR}/all.log"
ARCHIVE_FILE="${EXPORT_DIR}/flower-logs-${LOG_ROLE}-${RUN_ID}.tar.gz"
GIT_COMMIT="$(git -C "${ROOT_DIR}" rev-parse HEAD 2>/dev/null || true)"
RADIO_FIRMWARE=""

mkdir -p "${RUN_DIR}"
exec > >(tee -a "${RUN_LOG}") 2>&1

echo "[run] log_bundle=${RUN_DIR}"

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
placeholder_mesh_ids = {
    "",
    "!00000001",
    "!00000002",
    "!00000003",
    "!CLIENT_RADIO_ID",
    "!CENTRAL_RADIO_ID",
}

def is_loopback(host):
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False

if role not in {"master", "slave"}:
    errors.append("app.role must be master or slave")
if bridge.get("enabled") is not True:
    errors.append("bridge.enabled must be true")
if not is_loopback(bridge.get("listen_host")):
    errors.append("bridge.listen_host must be loopback only, usually 127.0.0.1")
if not is_loopback(bridge.get("upstream_host")):
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

redact_config() {
  "${PY}" - "${CONFIG}" "${REDACTED_CONFIG}" <<'PY'
import sys
from pathlib import Path
import yaml

source = Path(sys.argv[1])
target = Path(sys.argv[2])
with source.open(encoding="utf-8") as fh:
    cfg = yaml.safe_load(fh)

def redact(mapping, key):
    if isinstance(mapping, dict) and key in mapping:
        mapping[key] = "<redacted>" if mapping[key] else ""

redact(cfg.get("network", {}), "network_password")
redact(cfg.get("radio", {}), "channel_psk_base64")
redact(cfg.get("telegram", {}), "bot_token")
redact(cfg.get("telegram", {}), "allowed_chat_id")

target.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
PY
}

write_metadata() {
  {
    echo "run_id=${RUN_ID}"
    echo "role=${ROLE}"
    echo "log_role=${LOG_ROLE}"
    echo "started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "hostname=$(hostname 2>/dev/null || true)"
    echo "uname=$(uname -a 2>/dev/null || true)"
    echo "cwd=${ROOT_DIR}"
    echo "config=${CONFIG}"
    echo "listen=${LISTEN_HOST}:${LISTEN_PORT}"
    echo "upstream=${UPSTREAM_HOST}:${UPSTREAM_PORT}"
    echo "rounds=${ROUNDS}"
    echo "logical_clients=${LOGICAL_CLIENTS}"
    echo "evaluate=${EVALUATE}"
    echo "model_bytes=${MODEL_BYTES:-benchmark-default}"
    echo "weights_npz=${WEIGHTS_NPZ:-}"
    echo "bridge_payload_bytes=${BRIDGE_PAYLOAD_BYTES}"
    echo "bridge_window_size=${BRIDGE_WINDOW_SIZE}"
    echo "bridge_ack_timeout_seconds=${BRIDGE_ACK_TIMEOUT_SECONDS}"
    echo "bridge_control_timeout_seconds=${BRIDGE_CONTROL_TIMEOUT_SECONDS}"
    echo "bridge_max_retries=${BRIDGE_MAX_RETRIES}"
    echo "bridge_frame_interval_ms=${BRIDGE_FRAME_INTERVAL_MS}"
    echo "bridge_poll_interval_ms=${BRIDGE_POLL_INTERVAL_MS}"
    echo "allow_peer_mismatch=${ALLOW_PEER_MISMATCH}"
    echo "status_interval=${STATUS_INTERVAL}"
    echo "bridge_metrics_interval=${BRIDGE_METRICS_INTERVAL}"
    echo "capture_radio_info=${CAPTURE_RADIO_INFO}"
    echo "export_log_archive=${EXPORT_LOG_ARCHIVE}"
    echo "archive_file=${ARCHIVE_FILE}"
    echo "offline_mode=1"
    echo "bridge_radio_destination_mode=${MESHNET_BRIDGE_RADIO_DESTINATION_MODE:-single_peer_broadcast}"
    echo "journal_since=${JOURNAL_SINCE}"
    echo "python=$("${PY}" --version 2>&1)"
    echo "meshtastic_cli=${MESHTASTIC_CLI}"
    echo "git_branch=$(git branch --show-current 2>/dev/null || true)"
    echo "git_commit=${GIT_COMMIT}"
    echo "git_remote=$(git remote get-url origin 2>/dev/null || true)"
    echo
    echo "git_status:"
    git status -sb 2>/dev/null || true
    echo
    echo "serial_devices:"
    ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true
  } >> "${METADATA_FILE}" 2>&1
}

capture_systemd_logs() {
  local phase="$1"
  local outfile="$2"
  {
    echo "===== systemd ${phase} $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="
    if command -v journalctl >/dev/null 2>&1; then
      journalctl --no-pager --since "${JOURNAL_SINCE}" \
        -u meshnet-flower-bridge \
        -u meshnet \
        -u meshnet-telegram 2>&1 || true
    else
      echo "journalctl unavailable on this host"
    fi
  } >> "${outfile}" 2>&1
}

redact_log_file() {
  local path="$1"
  [[ -f "${path}" ]] || return
  "${PY}" - "${path}" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8", errors="replace")
secret_keys = (
    "privateKey",
    "publicKey",
    "password",
    "psk",
    "wifiPsk",
    "fixedPin",
    "bot_token",
    "allowed_chat_id",
    "channel_psk_base64",
)
for key in secret_keys:
    text = re.sub(
        rf'("{re.escape(key)}"\s*:\s*)"[^"]*"',
        rf'\1"<redacted>"',
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf'("{re.escape(key)}"\s*:\s*)[0-9]+',
        rf'\1"<redacted>"',
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf'({re.escape(key)}\s*[=:]\s*)\S+',
        rf'\1<redacted>',
        text,
        flags=re.IGNORECASE,
    )
text = re.sub(
    r'(Primary channel URL:\s*)\S+',
    r'\1<redacted>',
    text,
    flags=re.IGNORECASE,
)
path.write_text(text, encoding="utf-8")
PY
}

capture_radio_info() {
  {
    echo "===== radio-info $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="
    if [[ "${CAPTURE_RADIO_INFO}" == "0" || "${CAPTURE_RADIO_INFO}" == "false" ]]; then
      echo "disabled by CAPTURE_RADIO_INFO=${CAPTURE_RADIO_INFO}"
      return
    fi
    local port_output=""
    local detect_status=0
    set +e
    port_output="$("${MESHNET}" detect --plain 2>&1)"
    detect_status=$?
    set -e
    echo "detect_exit=${detect_status}"
    echo "detect_output=${port_output}"
    if [[ "${detect_status}" -ne 0 || "${port_output}" != /dev/* ]]; then
      echo "radio info skipped because serial detect did not return a /dev path"
      return
    fi
    if [[ ! -f "${MESHTASTIC_INFO}" ]]; then
      echo "radio info skipped because offline info wrapper is missing: ${MESHTASTIC_INFO}"
      return
    fi
    if command -v timeout >/dev/null 2>&1; then
      timeout 90 "${PY}" "${MESHTASTIC_INFO}" "${port_output}" 2>&1 || true
    else
      "${PY}" "${MESHTASTIC_INFO}" "${port_output}" 2>&1 || true
    fi
  } >> "${RADIO_INFO_LOG}" 2>&1
  redact_log_file "${RADIO_INFO_LOG}"
}

read_radio_firmware() {
  "${PY}" - "${RADIO_INFO_LOG}" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
text = path.read_text(encoding="utf-8", errors="replace")
match = re.search(r'"firmwareVersion"\s*:\s*"([^"]+)"', text)
print(match.group(1) if match else "")
PY
}

write_all_log() {
  local tmp="${ALL_LOG}.tmp"
  {
    for file in \
      "${METADATA_FILE}" \
      "${REDACTED_CONFIG}" \
      "${RADIO_INFO_LOG}" \
      "${SYSTEMD_BEFORE_LOG}" \
      "${SERVICES_BEFORE_FILE}" \
      "${STATUS_LOG}" \
      "${STATUS_JSONL}" \
      "${BENCH_LOG}" \
      "${BENCH_JSONL}" \
      "${BRIDGE_LOG}" \
      "${METRICS_FINAL}" \
      "${SYSTEMD_AFTER_LOG}" \
      "${RUN_LOG}"
    do
      if [[ -f "${file}" ]]; then
        echo
        echo "===== $(basename "${file}") ====="
        cat "${file}"
      fi
    done
  } > "${tmp}" 2>&1 || true
  mv "${tmp}" "${ALL_LOG}" 2>/dev/null || true
}

export_log_archive() {
  if [[ "${EXPORT_LOG_ARCHIVE}" == "0" || "${EXPORT_LOG_ARCHIVE}" == "false" || "${EXPORT_LOG_ARCHIVE}" == "no" ]]; then
    return
  fi
  mkdir -p "${EXPORT_DIR}"
  if tar -C "${ROOT_DIR}" -czf "${ARCHIVE_FILE}" "logs/${LOG_ROLE}/${RUN_ID}" 2>/dev/null; then
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum "${ARCHIVE_FILE}" > "${ARCHIVE_FILE}.sha256" 2>/dev/null || true
    elif command -v shasum >/dev/null 2>&1; then
      shasum -a 256 "${ARCHIVE_FILE}" > "${ARCHIVE_FILE}.sha256" 2>/dev/null || true
    fi
    echo "[run] archive=${ARCHIVE_FILE}"
  else
    echo "[run] archive_failed=${ARCHIVE_FILE}"
  fi
}

ACTIVE_SERVICES=()
stop_services() {
  : > "${SERVICES_BEFORE_FILE}"
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl=unavailable" >> "${SERVICES_BEFORE_FILE}"
    return
  fi
  local service state
  for service in meshnet-flower-bridge meshnet meshnet-telegram; do
    state="$(systemctl is-active "${service}" 2>/dev/null || true)"
    echo "${service}=${state:-unknown}" >> "${SERVICES_BEFORE_FILE}"
    if [[ "${state}" == "active" ]]; then
      ACTIVE_SERVICES+=("${service}")
      sudo systemctl stop "${service}" 2>/dev/null || true
    fi
  done
}

restore_services() {
  if [[ "${#ACTIVE_SERVICES[@]}" -eq 0 ]]; then
    return
  fi
  local service restore_state
  for service in "${ACTIVE_SERVICES[@]}"; do
    if sudo systemctl start "${service}" 2>/dev/null; then
      restore_state="restored"
    else
      restore_state="restore_failed"
    fi
    echo "${service}_after=${restore_state}" >> "${SERVICES_BEFORE_FILE}"
  done
}

cleanup_done=0
cleanup() {
  local status="${1:-$?}"
  if [[ "${cleanup_done}" == "1" ]]; then
    return
  fi
  cleanup_done=1
  if [[ -n "${STATUS_PID:-}" ]] && kill -0 "${STATUS_PID}" 2>/dev/null; then
    kill "${STATUS_PID}" 2>/dev/null || true
    wait "${STATUS_PID}" 2>/dev/null || true
  fi
  if [[ -n "${BRIDGE_PID:-}" ]] && kill -0 "${BRIDGE_PID}" 2>/dev/null; then
    kill "${BRIDGE_PID}" 2>/dev/null || true
    wait "${BRIDGE_PID}" 2>/dev/null || true
  fi
  if [[ -f "${METRICS_FILE:-}" ]]; then
    cp "${METRICS_FILE}" "${METRICS_FINAL}" 2>/dev/null || true
  fi
  restore_services
  capture_systemd_logs "after" "${SYSTEMD_AFTER_LOG}"
  {
    echo
    echo "finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "exit_status=${status}"
    if [[ "${status}" == "130" || "${status}" == "143" ]]; then
      echo "run_outcome=interrupted"
    elif [[ "${status}" == "0" ]]; then
      echo "run_outcome=completed"
    else
      echo "run_outcome=failed"
    fi
  } >> "${METADATA_FILE}" 2>&1 || true
  echo "[run] log_bundle=${RUN_DIR}"
  echo "[run] all_log=${ALL_LOG}"
  echo "[run] archive=${ARCHIVE_FILE}"
  echo "[run] manual_push_latest_logs=PUBLISH_FLOWER_LOGS=1 ./scripts/push-latest-flower-logs.sh ${LOG_ROLE}"
  write_all_log
  export_log_archive
}
trap 'status=$?; cleanup "${status}"; exit "${status}"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_status_log() {
  if [[ "${STATUS_INTERVAL}" == "0" || "${STATUS_INTERVAL}" == "false" ]]; then
    return
  fi
  echo "[run] concise status every ${STATUS_INTERVAL}s; set STATUS_INTERVAL=0 to hide it"
  "${PY}" - "${METRICS_FILE}" "${STATUS_INTERVAL}" "${ROLE}" "${STATUS_LOG}" "${STATUS_JSONL}" <<'PY' &
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
interval = float(sys.argv[2])
role = sys.argv[3]
status_log = Path(sys.argv[4])
status_jsonl = Path(sys.argv[5])
previous = {}
stalled_samples = 0

keys = [
    "active_connections",
    "pending_bytes",
    "frames_sent",
    "frames_received",
    "control_frames_attempted",
    "control_frames_sent",
    "control_frames_received",
    "local_bytes_received",
    "local_bytes_sent",
    "stream_bytes_queued",
    "stream_send_window_calls",
    "stream_send_window_active",
    "stream_send_window_empty",
    "stream_send_window_errors",
    "stream_window_bytes_sent",
    "data_bytes_attempted",
    "data_bytes_sent",
    "data_bytes_received",
    "acknowledgements_sent",
    "acknowledgements_received",
    "retransmitted_frames",
    "invalid_frames",
    "radio_broadcast_frames_sent",
    "radio_ack_received",
    "radio_ack_timeouts",
    "radio_naks",
    "sessions_opened",
    "sessions_reset",
]

def number(metrics, key):
    return int(metrics.get(key, 0) or 0)

def diagnose(metrics, previous_metrics, stalled_count):
    opened = number(metrics, "sessions_opened")
    conns = number(metrics, "active_connections")
    local_in = number(metrics, "local_bytes_received")
    queued = number(metrics, "stream_bytes_queued")
    windows = number(metrics, "stream_send_window_calls")
    active = number(metrics, "stream_send_window_active")
    errors = number(metrics, "stream_send_window_errors")
    data_try = number(metrics, "data_bytes_attempted")
    data_tx = number(metrics, "data_bytes_sent")
    data_rx = number(metrics, "data_bytes_received")
    local_out = number(metrics, "local_bytes_sent")
    ctrl_tx = number(metrics, "control_frames_sent")
    ctrl_rx = number(metrics, "control_frames_received")
    resets = number(metrics, "sessions_reset")
    radio_ack_timeouts = number(metrics, "radio_ack_timeouts")
    radio_naks = number(metrics, "radio_naks")
    progress = any(
        number(metrics, key) > number(previous_metrics, key)
        for key in (
            "frames_sent",
            "frames_received",
            "stream_window_bytes_sent",
            "data_bytes_received",
            "local_bytes_sent",
            "acknowledgements_received",
        )
    )

    if resets:
        return "session_reset"
    if radio_naks:
        return "radio_nak"
    if radio_ack_timeouts and ctrl_tx and not ctrl_rx:
        return "radio_ack_timeout_no_peer_rx"
    if opened == 0 or conns == 0:
        if ctrl_tx and not ctrl_rx:
            return "control_tx_no_peer_rx"
        return "tunnel_not_open"
    if role == "master" and local_in == 0 and data_rx == 0:
        return "central_waiting_for_client_hello"
    if role == "slave" and local_in == 0:
        return "client_no_local_tcp_bytes"
    if local_in > 0 and queued == 0:
        return "local_bytes_not_queued"
    if errors:
        return "send_window_error"
    if progress:
        return "transfer_progress"
    if active > 0:
        return "send_window_active"
    if queued > 0 and windows == 0 and stalled_count >= 3:
        return "queued_no_send_window"
    if windows > 0 and data_try == 0 and stalled_count >= 3:
        return "send_window_entered_no_data_try"
    if windows > 0 and data_try == 0:
        return "send_window_empty_or_closed"
    if (
        data_tx > 0
        and data_rx == 0
        and local_out == 0
        and number(metrics, "acknowledgements_received") == 0
        and stalled_count >= 3
    ):
        return "data_tx_no_peer_rx"
    if data_rx > 0 and local_out == 0 and stalled_count >= 3:
        return "data_rx_not_written_to_tcp"
    if local_out > 0:
        return "active_waiting"
    if stalled_count >= 3 and number(metrics, "pending_bytes") > 0:
        return "transfer_stalled"
    return "active_waiting"

def append_line(line):
    print(line, flush=True)
    with status_log.open("a", encoding="utf-8") as fh:
        print(line, file=fh, flush=True)

def append_json(event):
    with status_jsonl.open("a", encoding="utf-8") as fh:
        json.dump(event, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()

while True:
    time.sleep(interval)
    ts = datetime.now(timezone.utc).isoformat()
    if not path.exists():
        line = "[status] waiting_for_bridge_metrics"
        append_line(line)
        append_json({"ts_utc": ts, "event": "waiting_for_bridge_metrics"})
        continue
    try:
        metrics = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        line = f"[status] metrics_read_failed={exc}"
        append_line(line)
        append_json({"ts_utc": ts, "event": "metrics_read_failed", "error": str(exc)})
        continue

    def delta(key):
        return number(metrics, key) - number(previous, key)

    made_progress = any(
        delta(key) > 0
        for key in (
            "frames_sent",
            "frames_received",
            "stream_window_bytes_sent",
            "data_bytes_received",
            "local_bytes_sent",
            "acknowledgements_received",
        )
    )
    if number(metrics, "active_connections") > 0 and not made_progress:
        stalled_samples += 1
    else:
        stalled_samples = 0

    diagnosis = diagnose(metrics, previous, stalled_samples)
    uptime = metrics.get("uptime_seconds", 0)
    delta_map = {key: delta(key) for key in keys}
    queued_total = number(metrics, "stream_bytes_queued")
    tx_confirmed = number(metrics, "stream_window_bytes_sent")
    tx_rate = max(0.0, delta("stream_window_bytes_sent") / interval)
    rx_rate = max(0.0, delta("local_bytes_sent") / interval)
    progress_percent = (
        min(100.0, 100.0 * tx_confirmed / queued_total)
        if queued_total
        else 0.0
    )
    eta_seconds = (
        number(metrics, "pending_bytes") / tx_rate
        if tx_rate > 0 and number(metrics, "pending_bytes") > 0
        else 0.0
    )
    line = (
        "[status] "
        f"diagnosis={diagnosis} "
        f"up={uptime}s "
        f"conns={number(metrics, 'active_connections')} "
        f"pending={number(metrics, 'pending_bytes')}B "
        f"queue_progress={tx_confirmed}/{queued_total}B({progress_percent:.1f}%) "
        f"tx_rate={tx_rate:.1f}B/s "
        f"rx_rate={rx_rate:.1f}B/s "
        f"eta={eta_seconds:.0f}s "
        f"packets_tx={number(metrics, 'frames_sent')}(+{delta('frames_sent')}) "
        f"packets_rx={number(metrics, 'frames_received')}(+{delta('frames_received')}) "
        f"ctrl_try={number(metrics, 'control_frames_attempted')}(+{delta('control_frames_attempted')}) "
        f"ctrl_tx={number(metrics, 'control_frames_sent')}(+{delta('control_frames_sent')}) "
        f"ctrl_rx={number(metrics, 'control_frames_received')}(+{delta('control_frames_received')}) "
        f"local_in={number(metrics, 'local_bytes_received')}B(+{delta('local_bytes_received')}) "
        f"local_out={number(metrics, 'local_bytes_sent')}B(+{delta('local_bytes_sent')}) "
        f"queued={number(metrics, 'stream_bytes_queued')}B(+{delta('stream_bytes_queued')}) "
        f"windows={number(metrics, 'stream_send_window_calls')}(+{delta('stream_send_window_calls')}) "
        f"send_active={number(metrics, 'stream_send_window_active')} "
        f"win_empty={number(metrics, 'stream_send_window_empty')}(+{delta('stream_send_window_empty')}) "
        f"win_errors={number(metrics, 'stream_send_window_errors')}(+{delta('stream_send_window_errors')}) "
        f"win_bytes={number(metrics, 'stream_window_bytes_sent')}B(+{delta('stream_window_bytes_sent')}) "
        f"data_try={number(metrics, 'data_bytes_attempted')}B(+{delta('data_bytes_attempted')}) "
        f"data_tx={number(metrics, 'data_bytes_sent')}B(+{delta('data_bytes_sent')}) "
        f"data_rx={number(metrics, 'data_bytes_received')}B(+{delta('data_bytes_received')}) "
        f"ack_tx={number(metrics, 'acknowledgements_sent')}(+{delta('acknowledgements_sent')}) "
        f"ack_rx={number(metrics, 'acknowledgements_received')}(+{delta('acknowledgements_received')}) "
        f"retrans={number(metrics, 'retransmitted_frames')}(+{delta('retransmitted_frames')}) "
        f"invalid={number(metrics, 'invalid_frames')}(+{delta('invalid_frames')}) "
        f"rbcast={number(metrics, 'radio_broadcast_frames_sent')}(+{delta('radio_broadcast_frames_sent')}) "
        f"rack={number(metrics, 'radio_ack_received')}(+{delta('radio_ack_received')}) "
        f"rack_to={number(metrics, 'radio_ack_timeouts')}(+{delta('radio_ack_timeouts')}) "
        f"rnak={number(metrics, 'radio_naks')}(+{delta('radio_naks')}) "
        f"opened={number(metrics, 'sessions_opened')} "
        f"resets={number(metrics, 'sessions_reset')}"
    )
    append_line(line)
    append_json(
        {
            "ts_utc": ts,
            "event": "status",
            "diagnosis": diagnosis,
            "role": role,
            "metrics": metrics,
            "delta": delta_map,
        }
    )
    previous = metrics
PY
  STATUS_PID="$!"
}

run_benchmark() {
  local status=0
  set +e
  {
    echo "===== benchmark command $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="
    printf "[command]"
    printf " %q" "$@"
    echo
    "$@"
  } 2>&1 | tee -a "${BENCH_LOG}"
  status="${PIPESTATUS[0]}"
  set -e
  if [[ "${status}" -ne 0 ]]; then
    echo "[run] benchmark failed with exit ${status}. Last bridge log lines:" >&2
    tail -n 160 "${BRIDGE_LOG}" >&2 || true
    exit "${status}"
  fi
}

redact_config
write_metadata
capture_systemd_logs "before" "${SYSTEMD_BEFORE_LOG}"

echo "[run] role=${ROLE} config=${CONFIG}"
echo "[run] offline_mode=1 (loopback TCP + attached LoRa only; no auto-publish)"
echo "[run] rounds=${ROUNDS} logical_clients=${LOGICAL_CLIENTS} evaluate=${EVALUATE}"
echo "[run] bridge_payload_bytes=${BRIDGE_PAYLOAD_BYTES} bridge_window_size=${BRIDGE_WINDOW_SIZE} ack_timeout_seconds=${BRIDGE_ACK_TIMEOUT_SECONDS} control_timeout_seconds=${BRIDGE_CONTROL_TIMEOUT_SECONDS} max_retries=${BRIDGE_MAX_RETRIES} frame_interval_ms=${BRIDGE_FRAME_INTERVAL_MS} poll_interval_ms=${BRIDGE_POLL_INTERVAL_MS}"
if [[ "${ALLOW_PEER_MISMATCH}" == "1" || "${ALLOW_PEER_MISMATCH}" == "true" ]]; then
  echo "[run] peer_match=warn (firmware/Git mismatches are logged and the run continues)"
else
  echo "[run] peer_match=strict (firmware/Git mismatches reject the connection)"
fi
echo "[run] status_interval=${STATUS_INTERVAL}"
if [[ -n "${WEIGHTS_NPZ}" ]]; then
  echo "[run] weights_npz=${WEIGHTS_NPZ}"
elif [[ -n "${MODEL_BYTES}" ]]; then
  echo "[run] model_bytes=${MODEL_BYTES}"
else
  echo "[run] model_bytes=benchmark-default"
fi
echo "[run] run_log=${RUN_LOG}"
echo "[run] status_log=${STATUS_LOG}"
echo "[run] bridge_log=${BRIDGE_LOG}"
echo "[run] benchmark_log=${BENCH_LOG}"
echo "[run] jsonl=${BENCH_JSONL}"
echo "[run] metrics_file=${METRICS_FILE}"

stop_services
capture_radio_info
RADIO_FIRMWARE="$(read_radio_firmware)"
echo "radio_firmware=${RADIO_FIRMWARE:-unavailable}" >> "${METADATA_FILE}"
if [[ -z "${RADIO_FIRMWARE}" ]]; then
  echo "[run] WARNING: radio firmware version unavailable; peer firmware guard is incomplete"
fi
rm -f "${METRICS_FILE}"

echo "[run] starting bridge"
MESHNET_BRIDGE_PAYLOAD_BYTES="${BRIDGE_PAYLOAD_BYTES}" \
  MESHNET_BRIDGE_WINDOW_SIZE="${BRIDGE_WINDOW_SIZE}" \
  MESHNET_BRIDGE_ACK_TIMEOUT_SECONDS="${BRIDGE_ACK_TIMEOUT_SECONDS}" \
  MESHNET_BRIDGE_CONTROL_TIMEOUT_SECONDS="${BRIDGE_CONTROL_TIMEOUT_SECONDS}" \
  MESHNET_BRIDGE_MAX_RETRIES="${BRIDGE_MAX_RETRIES}" \
  MESHNET_BRIDGE_FRAME_INTERVAL_MS="${BRIDGE_FRAME_INTERVAL_MS}" \
  MESHNET_BRIDGE_POLL_INTERVAL_MS="${BRIDGE_POLL_INTERVAL_MS}" \
  MESHNET_BRIDGE_METRICS_INTERVAL_SECONDS="${BRIDGE_METRICS_INTERVAL}" \
  "${MESHNET}" bridge --config "${CONFIG}" >"${BRIDGE_LOG}" 2>&1 &
BRIDGE_PID="$!"
sleep 5

if ! kill -0 "${BRIDGE_PID}" 2>/dev/null; then
  echo "[run] bridge exited early. Last bridge log lines:" >&2
  tail -n 120 "${BRIDGE_LOG}" >&2 || true
  exit 1
fi

start_status_log

BENCH_ARGS=(
  --payload-bytes "${BRIDGE_PAYLOAD_BYTES}"
  --window-size "${BRIDGE_WINDOW_SIZE}"
  --frame-interval-ms "${BRIDGE_FRAME_INTERVAL_MS}"
  --poll-interval-ms "${BRIDGE_POLL_INTERVAL_MS}"
)
PEER_ARGS=(
  --git-commit "${GIT_COMMIT}"
  --firmware-version "${RADIO_FIRMWARE}"
)
if [[ "${ALLOW_PEER_MISMATCH}" == "1" || "${ALLOW_PEER_MISMATCH}" == "true" ]]; then
  PEER_ARGS+=(--allow-peer-mismatch)
else
  PEER_ARGS+=(--strict-peer-match)
fi
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
    "${PEER_ARGS[@]}" \
    "${BENCH_ARGS[@]}"
elif [[ "${ROLE}" == "slave" ]]; then
  echo "[run] client benchmark connecting through bridge"
  run_benchmark "${PY}" scripts/flower_round_benchmark.py client \
    --host "${LISTEN_HOST}" \
    --port "${LISTEN_PORT}" \
    --jsonl "${BENCH_JSONL}" \
    "${PEER_ARGS[@]}"
else
  echo "[run] unsupported app.role=${ROLE}; expected master or slave" >&2
  exit 1
fi

echo "[run] done"
