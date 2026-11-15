#!/bin/sh
set -eu

PORT=8123
# HAOS monte /data mais on peut surcharger via HAOS_DATA_DIR pour les tests
export HAOS_DATA_DIR="${HAOS_DATA_DIR:-/data}"
echo "[vinted] écoute sur 0.0.0.0:${PORT}, data dir: ${HAOS_DATA_DIR}"
exec python3 /app/app.py --port ${PORT} --host 0.0.0.0