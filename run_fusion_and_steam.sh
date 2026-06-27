#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="${ENV_NAME:-controller-fusion}"
VIRTUAL_VENDOR="${VIRTUAL_VENDOR:-0xF155}"
VIRTUAL_PRODUCT="${VIRTUAL_PRODUCT:-0x0001}"
VIRTUAL_NAME="${VIRTUAL_NAME:-Controller Fusion Prototype}"
FUSION_RESTART_DELAY="${FUSION_RESTART_DELAY:-2}"
LOG_DIR="${LOG_DIR:-${SCRIPT_DIR}/logs}"
FUSION_LOG="${FUSION_LOG:-${LOG_DIR}/fusion-and-steam.log}"
STEAM_BIN="${STEAM_BIN:-steam}"
STEAM_START_TIMEOUT="${STEAM_START_TIMEOUT:-20}"
STEAM_SHUTDOWN_TIMEOUT="${STEAM_SHUTDOWN_TIMEOUT:-30}"
STEAM_TERMINATE_TIMEOUT="${STEAM_TERMINATE_TIMEOUT:-10}"

find_conda_sh() {
  if [[ -n "${CONDA_SH:-}" ]]; then
    printf '%s\n' "${CONDA_SH}"
    return 0
  fi
  if ! command -v conda >/dev/null 2>&1; then
    return 1
  fi
  printf '%s/etc/profile.d/conda.sh\n' "$(conda info --base)"
}

CONDA_SH="$(find_conda_sh || true)"

if [[ ! -f "${CONDA_SH}" ]]; then
  echo "error: conda activation script not found. Set CONDA_SH or add conda to PATH." >&2
  exit 1
fi

source "${CONDA_SH}"
conda activate "${ENV_NAME}"

mkdir -p "${LOG_DIR}"

steam_pids() {
  ps -u "$(id -u)" -o pid= -o stat= -o comm= -o args= |
    awk '
      $2 ~ /^Z/ { next }
      {
        pid = $1
        comm = $3
        args = $0
        sub(/^[[:space:]]*[0-9]+[[:space:]]+[^[:space:]]+[[:space:]]+[^[:space:]]+[[:space:]]+/, "", args)

        if (comm == "steam" ||
            args ~ /(^|\/)steam\.sh([[:space:]]|$)/ ||
            args ~ /(^|\/)steam([[:space:]]|$)/) {
          print pid
        }
      }
    '
}

steam_is_running() {
  [[ -n "$(steam_pids)" ]]
}

wait_for_steam() {
  local deadline=$((SECONDS + STEAM_START_TIMEOUT))

  while (( SECONDS < deadline )); do
    if steam_is_running; then
      return 0
    fi
    sleep 1
  done

  return 1
}

wait_for_steam_exit() {
  local timeout="${1:-${STEAM_SHUTDOWN_TIMEOUT}}"
  local deadline=$((SECONDS + timeout))

  while (( SECONDS < deadline )); do
    if ! steam_is_running; then
      return 0
    fi
    sleep 1
  done

  return 1
}

stop_existing_steam() {
  echo "Steam is already running without the SDL filter; asking it to shut down first." >&2
  "${STEAM_BIN}" -shutdown >/dev/null 2>&1 || true

  if wait_for_steam_exit; then
    return 0
  fi

  echo "Steam did not exit after ${STEAM_SHUTDOWN_TIMEOUT}s; terminating remaining client PIDs." >&2
  steam_pids | xargs -r kill -TERM

  if wait_for_steam_exit "${STEAM_TERMINATE_TIMEOUT}"; then
    return 0
  fi

  echo "Steam still did not exit; force-killing remaining client PIDs." >&2
  steam_pids | xargs -r kill -KILL

  if wait_for_steam_exit 5; then
    return 0
  fi

  echo "error: Steam did not exit." >&2
  echo "       Remaining Steam client PIDs: $(steam_pids | xargs echo)" >&2
  return 1
}

start_fusion() {
  echo "[$(date --iso-8601=seconds)] starting controller fusion" | tee -a "${FUSION_LOG}"
  PYTHONUNBUFFERED=1 "${SCRIPT_DIR}/controller_fusion.py" \
    --virtual-vendor "${VIRTUAL_VENDOR}" \
    --virtual-product "${VIRTUAL_PRODUCT}" \
    --virtual-name "${VIRTUAL_NAME}" \
    --grab both \
    --steer-blend \
    "$@" >>"${FUSION_LOG}" 2>&1 &
  FUSION_PID=$!
}

cleanup() {
  if [[ -n "${FUSION_PID:-}" ]]; then
    kill "${FUSION_PID}" >/dev/null 2>&1 || true
    wait "${FUSION_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

if steam_is_running; then
  stop_existing_steam
fi

start_fusion "$@"
sleep 1
if ! kill -0 "${FUSION_PID}" >/dev/null 2>&1; then
  echo "error: controller fusion exited during startup. See ${FUSION_LOG}" >&2
  tail -n 80 "${FUSION_LOG}" >&2 || true
  exit 1
fi

echo "[$(date --iso-8601=seconds)] launching Steam with SDL limited to ${VIRTUAL_VENDOR}/${VIRTUAL_PRODUCT}" | tee -a "${FUSION_LOG}"
/usr/bin/env SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT="${VIRTUAL_VENDOR}/${VIRTUAL_PRODUCT}" "${STEAM_BIN}" &
STEAM_LAUNCHER_PID=$!

if ! wait_for_steam; then
  echo "error: Steam did not stay running after launch." >&2
  wait "${STEAM_LAUNCHER_PID}" >/dev/null 2>&1 || true
  exit 1
fi

while steam_is_running; do
  if ! kill -0 "${FUSION_PID}" >/dev/null 2>&1; then
    wait "${FUSION_PID}" >/dev/null 2>&1 || true
    echo "[$(date --iso-8601=seconds)] controller fusion exited; restarting in ${FUSION_RESTART_DELAY}s" | tee -a "${FUSION_LOG}"
    sleep "${FUSION_RESTART_DELAY}"
    start_fusion "$@"
  fi
  sleep 1
done

wait "${STEAM_LAUNCHER_PID}" >/dev/null 2>&1 || true
