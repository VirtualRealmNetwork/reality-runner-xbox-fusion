#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="${ENV_NAME:-controller-fusion}"
DEFAULT_VIRTUAL_VENDOR="${DEFAULT_VIRTUAL_VENDOR:-0xF155}"
DEFAULT_VIRTUAL_PRODUCT="${DEFAULT_VIRTUAL_PRODUCT:-0x0001}"

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

exec "${SCRIPT_DIR}/controller_fusion.py" \
  --virtual-vendor "${DEFAULT_VIRTUAL_VENDOR}" \
  --virtual-product "${DEFAULT_VIRTUAL_PRODUCT}" \
  "$@"
