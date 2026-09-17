#!/usr/bin/env bash
# macOS / Linux / Git Bash: creates .venv on first run, installs requirements, starts the app.
#   ./run.sh            # http://127.0.0.1:8000
#   PORT=8080 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
[ -d .venv ] || python3 -m venv .venv
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=.venv/Scripts/python.exe; fi
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example"; }
echo "LLM Compare: http://127.0.0.1:$PORT"
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload
