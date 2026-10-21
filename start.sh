#!/bin/zsh
# Starts the app at http://127.0.0.1:8000. Press Control+C in this window to stop it.
cd "$(dirname "$0")"
.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
