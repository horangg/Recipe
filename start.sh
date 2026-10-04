#!/bin/zsh
cd "$(dirname "$0")" && source ~/.zshrc && exec .venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
