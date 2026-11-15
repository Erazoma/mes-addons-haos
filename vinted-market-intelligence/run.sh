#!/bin/sh
set -eu

PORT=8123
echo "[vinted] écoute sur 0.0.0.0:${PORT}"
exec python3 /app/app.py --port ${PORT} --host 0.0.0.0