#!/usr/bin/env bash
# Launch the layer-painting window (browser front end + Python compositing core).
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
PORT="${1:-8765}"
echo "Painting window: http://127.0.0.1:${PORT}/"
exec python3 -m layerpaint.server --port "${PORT}"
