#!/usr/bin/env bash
# cleanup.sh — Ménage du dossier odoo-quiz (À RELIRE avant de lancer).
#
#   bash ~/odoo-quiz/cleanup.sh
#
# Ne supprime QUE des fichiers transitoires / reconstructibles, et demande
# confirmation. Ne touche NI au code, NI à questions.json, NI à data/odoo_docs.sqlite,
# NI aux embeddings, NI au pont .bridge/ (voir notes en fin de script).

set -u
cd "$(dirname "$0")" || exit 1

echo "Dossier      : $(pwd)"
echo "Taille avant : $(du -sh . 2>/dev/null | cut -f1)"
echo

# Transitoires sûrs (copies temporaires, logs, artefacts de debug/test)
SAFE=(
  "data/odoo_docs.sqlite.from-vps"   # copie temporaire rapatriée du VPS (~35 Mo)
  "generate_explanations.log"         # vieux log de génération
  "data/chunk_embedding_full.json"
  "data/chunk_embedding_full.txt"
  "data/chunk_sample_preview.json"
  "data/schema_inspection_report.md"
  "data/generated_pending_TEST"       # dossier de test
)

echo "=== Seront supprimés (transitoires, reconstructibles) ==="
for f in "${SAFE[@]}"; do [ -e "$f" ] && echo "  - $f ($(du -sh "$f" 2>/dev/null | cut -f1))"; done
echo "  - tous les __pycache__/"
echo "  - tous les .DS_Store"
echo "  - questions.json.bak.*  (sauvegardes anciennes)"
echo

read -r -p "Confirmer la suppression ? [o/N] " ans
case "$ans" in
  o|O|oui|OUI) ;;
  *) echo "Annulé."; exit 0 ;;
esac

for f in "${SAFE[@]}"; do rm -rf "$f"; done
find . -path ./.venv -prune -o -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
find . -path ./.venv -prune -o -name .DS_Store -delete 2>/dev/null
rm -f questions.json.bak.*

echo
echo "Taille après : $(du -sh . 2>/dev/null | cut -f1)"
echo "✅ Ménage terminé."
echo
echo "----------------------------------------------------------------------"
echo "NON touché par sécurité :"
echo "  • .bridge/  (pont Claude↔Mac). Arrête d'abord cmdbridge.sh (Ctrl+C),"
echo "    puis si tu veux :   rm -rf .bridge"
echo
echo "OPTIONNEL — états de pipeline (seulement si tu ne reprendras pas une"
echo "génération/jugement/traduction interrompus) :"
echo "  rm -f data/run_state.json data/judge_state.json data/translate_state.json"
echo "  rm -f data/dedup_log.jsonl data/insertion_log.jsonl data/rejected_questions.jsonl"
echo "  rm -rf data/generated_pending"
echo "----------------------------------------------------------------------"
