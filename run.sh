#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f config.json ]; then
  echo "❌ config.json manquant. Copie config.example.json et remplis-le :"
  echo "   cp config.example.json config.json"
  exit 1
fi

FLASK_PORT=$(python3 -c "import json; print(int(json.load(open('config.json')).get('flask',{}).get('port',5001)))")
FLASK_HOST=$(python3 -c "import json; print(json.load(open('config.json')).get('flask',{}).get('host','127.0.0.1'))")

# Crée un venv si besoin
if [ ! -d .venv ]; then
  echo "⏳ Création du virtualenv Python…"
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q flask

# Extrait les questions si absentes
QFILE="questions.json"
if [ ! -f "$QFILE" ] || [ ! -s "$QFILE" ]; then
  echo "📥 Extraction des questions depuis Odoo…"
  python3 extract_odoo.py
else
  NB=$(python3 -c "import json; d=json.load(open('$QFILE')); print(len(d.get('questions',[])))" 2>/dev/null || echo '?')
  echo "✅ $QFILE déjà présent ($NB questions)"
  if [[ -t 0 ]]; then
    read -p "Re-extraire depuis Odoo ? [o/N] " ans
    if [[ "$ans" =~ ^[Oo]$ ]]; then
      python3 extract_odoo.py
    fi
  fi
fi

# Génère les explications manquantes
NB_EXPL=$(python3 -c "
import json
d=json.load(open('$QFILE'))
qs=d.get('questions',[])
print(sum(1 for q in qs if not q.get('explanation')))
" 2>/dev/null || echo '0')

if [ "$NB_EXPL" -gt 0 ]; then
  echo ""
  echo "💡 $NB_EXPL questions sans explication."
  if [[ -t 0 ]]; then
    read -p "Générer les explications maintenant via Claude ? [O/n] " ans
    if [[ ! "$ans" =~ ^[Nn]$ ]]; then
      python3 generate_explanations.py
    fi
  fi
fi

echo ""
echo "🚀 Serveur Flask (lisez les lignes 🌐 dans le terminal Python ci-dessous)"
echo "   Quiz    : http://127.0.0.1:${FLASK_PORT}/"
echo "   Banque  : http://127.0.0.1:${FLASK_PORT}/banque"
echo "   Santé   : curl -s http://127.0.0.1:${FLASK_PORT}/health"
if [[ "$FLASK_HOST" == "0.0.0.0" ]]; then
  echo "   (config : host=0.0.0.0 — accès depuis le réseau : http://<IP-de-ce-mac>:${FLASK_PORT}/ )"
fi
echo "   Ctrl+C pour arrêter."
echo ""

# Ouvre le navigateur dès que Flask est prêt (toujours en local sur cette machine)
(until curl -s "http://127.0.0.1:${FLASK_PORT}/" > /dev/null 2>&1; do sleep 0.5; done && open "http://127.0.0.1:${FLASK_PORT}") &

python app.py
