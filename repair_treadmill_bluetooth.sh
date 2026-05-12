#!/usr/bin/env bash

set -euo pipefail

DEVICE_MAC_V1="AA:BB:CC:DD:EE:01"
DEVICE_MAC_V2="AA:BB:CC:DD:EE:02"
DEVICE_VERSION="${DEVICE_VERSION:-v2}"
DEVICE_MAC="${DEVICE_MAC:-}"
DEVICE_NAME="${DEVICE_NAME:-Reality Runner XINPUT}"
SCAN_SECONDS="${SCAN_SECONDS:-15}"
PAIR_SCAN_SECONDS="${PAIR_SCAN_SECONDS:-5}"
PAIR_TIMEOUT="${PAIR_TIMEOUT:-30}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-20}"
PAIR_RETRIES="${PAIR_RETRIES:-2}"
LOG_DIR="${LOG_DIR:-$(cd "$(dirname "$0")" && pwd)/logs}"
SCAN_ONLY=0
RUN_LOG=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Re-pair and reconnect the RealityRunner treadmill BLE device on this Linux machine.
This is intended for the case where the treadmill has recently been paired to
another computer and the local Linux bond may be stale.

Options:
  --v1                  Use the Reality Runner v1 MAC (${DEVICE_MAC_V1})
  --v2                  Use the Reality Runner v2 MAC (${DEVICE_MAC_V2}). Default
  --version VERSION     Select treadmill version: v1 or v2. Default: ${DEVICE_VERSION}
  --mac MAC             Override device MAC address
  --name NAME           Override expected device name. Default: ${DEVICE_NAME}
  --scan-seconds N      BLE scan duration in seconds. Default: ${SCAN_SECONDS}
  --log-dir DIR         Override log directory. Default: ${LOG_DIR}
  --scan-only           Scan for the treadmill and exit without pairing.
  --help                Show this help text.

Environment overrides:
  DEVICE_VERSION, DEVICE_MAC, DEVICE_NAME, SCAN_SECONDS, PAIR_SCAN_SECONDS, PAIR_TIMEOUT, CONNECT_TIMEOUT, PAIR_RETRIES, LOG_DIR

Examples:
  ./$(basename "$0")
  ./$(basename "$0") --v1
  DEVICE_VERSION=v1 ./$(basename "$0")
  ./$(basename "$0") --scan-only
  DEVICE_MAC=AA:BB:CC:DD:EE:FF ./$(basename "$0")
EOF
}

log() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

die() {
  log "ERROR: $*"
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

bt() {
  bluetoothctl "$@"
}

bt_timeout() {
  local seconds="$1"
  shift
  bluetoothctl --timeout "$seconds" "$@"
}

log_bt_failure() {
  local action="$1"
  local output="$2"

  log "${action} failed"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    printf '  %s\n' "$line"
  done <<<"$output"
}

device_info() {
  local output

  output="$(bt info "$DEVICE_MAC" 2>&1 || true)"
  if grep -Fqi "not available" <<<"$output"; then
    return 0
  fi

  if ! grep -Fq "Device ${DEVICE_MAC}" <<<"$output"; then
    return 0
  fi

  printf '%s\n' "$output"
}

info_has() {
  local needle="$1"
  device_info | grep -Fq "$needle"
}

find_mac_by_name() {
  local output

  output="$(bt devices 2>&1 || true)"
  while IFS= read -r line; do
    [[ "$line" == Device* ]] || continue
    if [[ "$line" == *"$DEVICE_NAME"* ]]; then
      printf '%s\n' "$line" | awk '{print $2}'
      return 0
    fi
  done <<<"$output"
  return 1
}

ensure_device_mac() {
  if [[ -n "$DEVICE_MAC" ]]; then
    return 0
  fi

  if DEVICE_MAC="$(find_mac_by_name)"; then
    log "Resolved ${DEVICE_NAME} to ${DEVICE_MAC}"
    return 0
  fi

  die "Could not resolve a device MAC for ${DEVICE_NAME}. Pass --mac or set DEVICE_MAC."
}

set_default_device_mac() {
  if [[ -n "$DEVICE_MAC" ]]; then
    DEVICE_VERSION="custom"
    return 0
  fi

  case "$DEVICE_VERSION" in
    v1|V1|1)
      DEVICE_VERSION="v1"
      DEVICE_MAC="$DEVICE_MAC_V1"
      ;;
    v2|V2|2)
      DEVICE_VERSION="v2"
      DEVICE_MAC="$DEVICE_MAC_V2"
      ;;
    *)
      die "DEVICE_VERSION must be v1 or v2, got: ${DEVICE_VERSION}"
      ;;
  esac
}

prepare_adapter() {
  log "Preparing Bluetooth adapter"
  bt scan off >/dev/null 2>&1 || true
  bt power on >/dev/null || true
  bt pairable on >/dev/null || true
  bt agent on >/dev/null || true
  bt default-agent >/dev/null || true
}

scan_for_device() {
  local scan_status

  if [[ -n "$DEVICE_MAC" ]]; then
    log "Scanning for ${DEVICE_NAME} (${DEVICE_MAC}) for ${SCAN_SECONDS}s"
  else
    log "Scanning for ${DEVICE_NAME} for ${SCAN_SECONDS}s"
  fi
  if {
    printf 'power on\n'
    printf 'agent on\n'
    printf 'default-agent\n'
    printf 'menu scan\n'
    printf 'transport le\n'
    printf 'duplicate-data on\n'
    printf 'back\n'
    printf 'scan on\n'
    sleep "$SCAN_SECONDS"
    printf 'scan off\n'
    printf 'quit\n'
  } | bluetoothctl >/dev/null 2>&1; then
    scan_status=0
  else
    scan_status=$?
  fi

  ensure_device_mac

  if bt devices | grep -Fqi "$DEVICE_MAC"; then
    log "Discovered device ${DEVICE_MAC}"
    return 0
  fi

  if info_has $'\tAlias:'; then
    log "Device ${DEVICE_MAC} is known to BlueZ from cache"
    return 0
  fi

  if (( scan_status != 0 )); then
    log "bluetoothctl scan exited with status ${scan_status}"
  fi

  die "Device ${DEVICE_MAC} was not found. Make sure the treadmill is powered on, in pairing mode, and disconnected from Windows."
}

remove_stale_device() {
  if ! device_info | grep -Fq "$DEVICE_MAC"; then
    log "No existing BlueZ record for ${DEVICE_MAC}"
    return 0
  fi

  log "Removing any stale bond or connection for ${DEVICE_MAC}"
  bt disconnect "$DEVICE_MAC" >/dev/null 2>&1 || true
  bt remove "$DEVICE_MAC" >/dev/null 2>&1 || true
  sleep 1
}

verify_connected() {
  info_has $'\tPaired: yes' &&
    info_has $'\tTrusted: yes' &&
    info_has $'\tConnected: yes'
}

active_scan_pair_sequence() {
  local output
  local status

  log "Running active-scan pair/connect sequence for ${DEVICE_MAC}"
  if output="$(
    {
      printf 'power on\n'
      printf 'agent on\n'
      printf 'default-agent\n'
      printf 'menu scan\n'
      printf 'transport le\n'
      printf 'duplicate-data on\n'
      printf 'back\n'
      printf 'scan on\n'
      sleep "$PAIR_SCAN_SECONDS"
      printf 'pair %s\n' "$DEVICE_MAC"
      sleep "$PAIR_TIMEOUT"
      printf 'trust %s\n' "$DEVICE_MAC"
      printf 'bearer %s le\n' "$DEVICE_MAC"
      printf 'connect %s\n' "$DEVICE_MAC"
      sleep "$CONNECT_TIMEOUT"
      printf 'info %s\n' "$DEVICE_MAC"
      printf 'scan off\n'
      printf 'quit\n'
    } | bluetoothctl 2>&1
  )"; then
    status=0
  else
    status=$?
  fi

  if (( status != 0 )); then
    log_bt_failure "Active-scan pair/connect sequence" "$output"
    return 1
  fi

  if grep -Eq 'Failed to (pair|connect|trust)|AuthenticationRejected|AuthenticationTimeout|le-connection-abort-by-local' <<<"$output"; then
    log_bt_failure "Active-scan pair/connect sequence" "$(printf '%s\n' "$output" | tail -n 80)"
    return 1
  fi

  verify_connected
}

pair_trust_connect_once() {
  active_scan_pair_sequence
}

pair_trust_connect() {
  local attempt

  for ((attempt = 1; attempt <= PAIR_RETRIES; attempt++)); do
    log "Repair attempt ${attempt}/${PAIR_RETRIES}"

    if pair_trust_connect_once; then
      return 0
    fi

    log "Attempt ${attempt} did not reach a fully connected state"
    remove_stale_device
    scan_for_device
  done

  die "Failed to pair and connect ${DEVICE_MAC}. Ensure the treadmill is not currently connected to Windows and restart its pairing mode."
}

print_summary() {
  local info
  info="$(device_info)"

  printf '\n'
  printf '%s\n' "$info"
}

setup_logging() {
  mkdir -p "$LOG_DIR"
  RUN_LOG="${LOG_DIR}/repair_$(date '+%Y%m%d_%H%M%S').log"
  exec > >(tee -a "$RUN_LOG") 2>&1
  log "Logging to ${RUN_LOG}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --v1)
      DEVICE_VERSION="v1"
      shift
      ;;
    --v2)
      DEVICE_VERSION="v2"
      shift
      ;;
    --version)
      [[ $# -ge 2 ]] || die "--version requires a value"
      DEVICE_VERSION="$2"
      shift 2
      ;;
    --mac)
      [[ $# -ge 2 ]] || die "--mac requires a value"
      DEVICE_MAC="$2"
      shift 2
      ;;
    --name)
      [[ $# -ge 2 ]] || die "--name requires a value"
      DEVICE_NAME="$2"
      shift 2
      ;;
    --scan-seconds)
      [[ $# -ge 2 ]] || die "--scan-seconds requires a value"
      SCAN_SECONDS="$2"
      shift 2
      ;;
    --log-dir)
      [[ $# -ge 2 ]] || die "--log-dir requires a value"
      LOG_DIR="$2"
      shift 2
      ;;
    --scan-only)
      SCAN_ONLY=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

require_cmd bluetoothctl
require_cmd tee

if ! [[ "$SCAN_SECONDS" =~ ^[0-9]+$ ]] || (( SCAN_SECONDS < 1 )); then
  die "SCAN_SECONDS must be a positive integer"
fi

if ! [[ "$PAIR_SCAN_SECONDS" =~ ^[0-9]+$ ]] || (( PAIR_SCAN_SECONDS < 1 )); then
  die "PAIR_SCAN_SECONDS must be a positive integer"
fi

if ! [[ "$PAIR_TIMEOUT" =~ ^[0-9]+$ ]] || (( PAIR_TIMEOUT < 1 )); then
  die "PAIR_TIMEOUT must be a positive integer"
fi

if ! [[ "$CONNECT_TIMEOUT" =~ ^[0-9]+$ ]] || (( CONNECT_TIMEOUT < 1 )); then
  die "CONNECT_TIMEOUT must be a positive integer"
fi

if ! [[ "$PAIR_RETRIES" =~ ^[0-9]+$ ]] || (( PAIR_RETRIES < 1 )); then
  die "PAIR_RETRIES must be a positive integer"
fi

set_default_device_mac
setup_logging
log "Target device: ${DEVICE_NAME} ${DEVICE_VERSION} (${DEVICE_MAC})"

prepare_adapter
scan_for_device

if (( SCAN_ONLY )); then
  log "Scan-only mode requested. No pairing changes were made."
  print_summary
  exit 0
fi

remove_stale_device
scan_for_device
pair_trust_connect

if ! verify_connected; then
  die "Bluetooth reported an incomplete final state after repair"
fi

log "Treadmill is paired, trusted, and connected"
print_summary
