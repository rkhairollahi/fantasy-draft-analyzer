#!/bin/bash
# Double-clickable launcher for the Fantasy Draft Analyzer.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/dfa ]; then
  echo "Setting up for the first time…"
  python3 -m venv .venv || exit 1
  .venv/bin/pip install -q -e . || exit 1
  .venv/bin/playwright install chromium
fi

# Reuse an already-running instance rather than failing on a busy port.
if curl -s --max-time 2 "http://127.0.0.1:8765/api/auth" >/dev/null 2>&1; then
  echo "Already running — opening the menu."
  open "http://127.0.0.1:8765/"
  sleep 1
  exit 0
fi

echo "Starting Fantasy Draft Analyzer…"
echo "Leave this window open while you use it. Close it or press Ctrl+C to stop."
exec .venv/bin/dfa app
