#!/usr/bin/env bash
# Arrête ce qui écoute sur le port Flask (config.json), puis relance app.py depuis la racine du projet.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -f config.json ]]; then
  echo "❌ config.json introuvable dans $ROOT"
  exit 1
fi
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3
PORT=$($PY -c "import json; print(int(json.load(open('config.json')).get('flask',{}).get('port',5001)))")
for p in $(lsof -ti tcp:"$PORT" 2>/dev/null || true); do
  kill "$p" 2>/dev/null || true
done
sleep 0.4
for p in $(lsof -ti tcp:"$PORT" 2>/dev/null || true); do
  kill -9 "$p" 2>/dev/null || true
done
source .venv/bin/activate
exec python app.py
