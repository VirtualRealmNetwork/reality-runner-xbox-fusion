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

start_fusion() {
  echo "[$(date --iso-8601=seconds)] starting controller fusion" | tee -a "${FUSION_LOG}"
  PYTHONUNBUFFERED=1 "${SCRIPT_DIR}/controller_fusion.py" \
    --virtual-vendor "${VIRTUAL_VENDOR}" \
    --virtual-product "${VIRTUAL_PRODUCT}" \
    --virtual-name "${VIRTUAL_NAME}" \
    --grab both \
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

start_fusion "$@"
sleep 1
if ! kill -0 "${FUSION_PID}" >/dev/null 2>&1; then
  echo "error: controller fusion exited during startup. See ${FUSION_LOG}" >&2
  tail -n 80 "${FUSION_LOG}" >&2 || true
  exit 1
fi

/usr/bin/env SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT="${VIRTUAL_VENDOR}/${VIRTUAL_PRODUCT}" steam &
STEAM_PID=$!

while kill -0 "${STEAM_PID}" >/dev/null 2>&1; do
  if ! kill -0 "${FUSION_PID}" >/dev/null 2>&1; then
    wait "${FUSION_PID}" >/dev/null 2>&1 || true
    echo "[$(date --iso-8601=seconds)] controller fusion exited; restarting in ${FUSION_RESTART_DELAY}s" | tee -a "${FUSION_LOG}"
    sleep "${FUSION_RESTART_DELAY}"
    start_fusion "$@"
  fi
  sleep 1
done

wait "${STEAM_PID}" >/dev/null 2>&1 || true
