#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run.sh — start the CIS Image Builder backend + UI.
#
# On the BUILDER CVM (real builds):
#   ./run.sh
#
# For LOCAL DEV (no packer / no Tencent metadata — simulates builds):
#   DRY_RUN=1 ./run.sh
#
# Environment overrides (see build_runner.py):
#   CIS_PIPELINE_DIR      default /opt/cis-pipeline/rhel-cis-official
#   CIS_GROUP_VARS_PATH   default <pipeline>/ansible/group_vars/all.yml
#   CIS_BUILD_LOG_DIR     default <pipeline>/logs
#   CIS_STORE_PATH        default ./builds_store.json
#   HOST / PORT           default 0.0.0.0 / 8080
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"

# ── Builder pipeline paths ────────────────────────────────────────────────────
# On this builder the Packer pipeline lives directly under /opt/cis-pipeline
# (i.e. `cd /opt/cis-pipeline && packer build packer/`). These match that layout.
# Override any of them if your builder differs.
export CIS_PIPELINE_DIR="${CIS_PIPELINE_DIR:-/opt/cis-pipeline}"
export CIS_PACKER_DIR="${CIS_PACKER_DIR:-$CIS_PIPELINE_DIR/packer}"
export CIS_GROUP_VARS_PATH="${CIS_GROUP_VARS_PATH:-$CIS_PIPELINE_DIR/ansible/group_vars/all.yml}"
export CIS_BUILD_LOG_DIR="${CIS_BUILD_LOG_DIR:-$CIS_PIPELINE_DIR/logs}"
# Base/source image id — the app refuses to record or delete this, so a failed
# build never gets tagged with (or offers to delete) the base image.
export CIS_BASE_IMAGE_ID="${CIS_BASE_IMAGE_ID:-img-4o7u81vk}"

if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "Installing backend requirements..."
  python3 -m pip install -r requirements.txt
fi

# ── Access control ────────────────────────────────────────────────────────────
# Set CIS_AUTH_TOKEN to require a shared token on all /api requests. STRONGLY
# recommended when the app is exposed via a CLB. If unset, auth is disabled.
if [ -z "${CIS_AUTH_TOKEN:-}" ]; then
  echo "WARNING: CIS_AUTH_TOKEN is not set — API auth is DISABLED."
  echo "         Anyone who can reach this endpoint can trigger real builds."
  echo "         Set CIS_AUTH_TOKEN=<secret> to require a token."
fi

echo "Starting CIS Image Builder on http://${HOST}:${PORT}  (DRY_RUN=${DRY_RUN:-0}, auth=$([ -n "${CIS_AUTH_TOKEN:-}" ] && echo on || echo off))"
exec python3 -m uvicorn app:app --host "$HOST" --port "$PORT"
