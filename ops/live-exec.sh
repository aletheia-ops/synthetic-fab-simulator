#!/bin/bash
# ExecStart of fab-live.service: run the attempt prepared by `fabctl start` (outputs/live-ops/launch.env).
set -euo pipefail
REPO=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
set -a; . "$REPO/outputs/live-ops/launch.env"; set +a
cd "$WT"
# An empty MANIFEST (fabctl launch) starts a fresh run.
exec "${FAB_PYTHON:-$REPO/.venv/bin/python}" -X faulthandler -m tools.measure_run --config "$ATTEMPT/config.json" \
  --report "$ATTEMPT/measurement.json" --progress --progress-interval 60 ${MANIFEST:+--resume-from "$MANIFEST"} \
  > "$ATTEMPT/console.json" 2> "$ATTEMPT/progress.jsonl"
