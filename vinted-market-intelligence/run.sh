#!/bin/sh
set -eu

# Variables d'environnement lues par app.py
export PORT=8124
export HOST=0.0.0.0
# HAOS monte /data mais on peut surcharger via HAOS_DATA_DIR pour les tests
export HAOS_DATA_DIR="${HAOS_DATA_DIR:-/data}"
echo "[vinted] écoute sur ${HOST}:${PORT}, data dir: ${HAOS_DATA_DIR}"
exec python3 /app/app.py
