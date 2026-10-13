#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -f .env ]]; then
  echo 'create .env from .env.example and configure Discord login first.' >&2
  exit 1
fi
set -a
source .env
set +a
if [[ -x venv/bin/python ]]; then
  exec venv/bin/python share_dashboard.py
fi
exec python share_dashboard.py
