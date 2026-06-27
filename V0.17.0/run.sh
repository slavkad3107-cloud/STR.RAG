#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
DATA="${PMOOS_DATA_DIR:-$HOME/.pmoos-rag}"
if [ -f "$DATA/venv/bin/activate" ]; then source "$DATA/venv/bin/activate"
elif [ -f .venv/bin/activate ]; then source .venv/bin/activate
else echo "Окружение не найдено — запустите ./install.sh"; exit 1; fi
streamlit run app/hub.py
