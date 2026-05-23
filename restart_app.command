#!/bin/bash
cd "$(dirname "$0")"

echo "=== Instalando dependencias ==="
pip3 install google-genai dropbox --break-system-packages -q

echo "=== Deteniendo app anterior ==="
pkill -f review_app.py 2>/dev/null
sleep 1

echo "=== Iniciando Gastos OWA ==="
python3 review_app.py
