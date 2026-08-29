#!/bin/bash
# Double-clickable launcher for the Fantasy Draft Analyzer.

# Resolve the real script location. $0 is the Desktop symlink, so a plain
# `dirname $0` lands on the Desktop and builds a venv in the wrong place.
SOURCE="${BASH_SOURCE[0]:-$0}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  case $SOURCE in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
APP_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
cd "$APP_DIR" || { echo "Cannot find the app directory."; sleep 5; exit 1; }

if [ ! -f pyproject.toml ]; then
  echo "This doesn't look like the analyzer directory: $APP_DIR"
  sleep 8; exit 1
fi

if [ ! -x .venv/bin/dfa ]; then
  echo "First run — setting up (a minute or two)…"
  python3 -m venv .venv || { echo "Could not create the virtualenv."; sleep 8; exit 1; }
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e . || { echo "Install failed."; sleep 8; exit 1; }
  .venv/bin/playwright install chromium
fi

# Reuse an instance that's already up rather than failing on a busy port.
if curl -s --max-time 2 "http://127.0.0.1:8765/api/auth" >/dev/null 2>&1; then
  echo "Already running — opening the menu."
  open "http://127.0.0.1:8765/"
  sleep 1
  exit 0
fi

echo "Starting Fantasy Draft Analyzer…"
echo "Keep this window open while you use it. Close it or press Ctrl+C to stop."
exec .venv/bin/dfa app
