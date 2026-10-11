#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_dir"

if [[ ! -x .venv/bin/python ]]; then
  echo 'Install dependencies first; see the Run section in README.md.' >&2
  exit 1
fi

port="${VID_DOWN_PORT:-8000}"
if [[ ! "$port" =~ ^[0-9]{1,5}$ ]] || (( 10#$port < 1 || 10#$port > 65535 )); then
  echo 'VID_DOWN_PORT must be an integer between 1 and 65535.' >&2
  exit 1
fi
export VID_DOWN_PORT="$port"

# Ignore proxy settings when checking this machine's loopback server.
if .venv/bin/python - <<'PY'
import json
import os
import sys
import urllib.request

port = int(os.environ['VID_DOWN_PORT'])
url = f'http://127.0.0.1:{port}'
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + '/api/health', timeout=2) as response:
        health = json.load(response)
    if health.get('ok') is True and 'ffmpeg' in health:
        print(f'vid_down is already running. Open {url} in your browser.')
        sys.exit(0)
except (OSError, ValueError):
    pass
print(f'Starting vid_down. Open {url} in your browser. Stop with Ctrl+C.')
sys.exit(1)
PY
then
  exit 0
fi

exec .venv/bin/python app.py
