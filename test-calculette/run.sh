#!/bin/sh
set -eu

PORT=8099
echo "[calculette] ecoute sur 0.0.0.0:${PORT}"
exec python3 -m http.server "${PORT}" --bind 0.0.0.0 --directory /app/www
