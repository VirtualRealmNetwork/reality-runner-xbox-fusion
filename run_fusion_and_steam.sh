#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="${ENV_NAME:-controller-fusion}"
VIRTUAL_VENDOR="${VIRTUAL_VENDOR:-0xF155}"
VIRTUAL_PRODUCT="${VIRTUAL_PRODUCT:-0x0001}"
VIRTUAL_NAME="${VIRTUAL_NAME:-Controller Fusion Prototype}"

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

cleanup() {
  if [[ -n "${FUSION_PID:-}" ]]; then
    kill "${FUSION_PID}" >/dev/null 2>&1 || true
    wait "${FUSION_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

"${SCRIPT_DIR}/controller_fusion.py" \
  --virtual-vendor "${VIRTUAL_VENDOR}" \
  --virtual-product "${VIRTUAL_PRODUCT}" \
  --virtual-name "${VIRTUAL_NAME}" \
  --grab both \
  "$@" &
FUSION_PID=$!

sleep 1

/usr/bin/env SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT="${VIRTUAL_VENDOR}/${VIRTUAL_PRODUCT}" steam
