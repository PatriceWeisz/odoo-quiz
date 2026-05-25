#!/usr/bin/env bash
#
# reencode_supervisor.sh — orchestration serveur du réencodage des embeddings.
#
# À lancer EN ROOT, détaché (le service ne peut être redémarré que par root, et
# senedoo n'a pas de sudo sans mot de passe) :
#
#   ssh root@VPS 'nohup bash /opt/odoo-quiz/scripts/reencode_supervisor.sh \
#       > /opt/odoo-quiz/logs/supervisor.log 2>&1 < /dev/null & echo PID=$!'
#
# Il exécute le réencodage EN TANT QUE senedoo (pour que les fichiers d'index
# restent la propriété de l'utilisateur applicatif), puis, en cas de succès,
# redémarre odoo-quiz et fait un contrôle santé. 100 % côté serveur : insensible
# à une coupure SSH / mise en veille du poste.
#
# Modèle par défaut surchargé par 1er argument :  reencode_supervisor.sh <model>

set -u

APP_DIR="/opt/odoo-quiz"
APP_USER="senedoo"
SERVICE="odoo-quiz"
MODEL="${1:-mixedbread-ai/mxbai-embed-large-v1}"

TS=$(date +%Y%m%dT%H%M%S)
LOG="${APP_DIR}/logs/reencode_${TS}.log"
mkdir -p "${APP_DIR}/logs"

echo "[$(date)] superviseur : réencodage modèle=${MODEL} -> ${LOG}"

sudo -u "${APP_USER}" bash -c \
  "cd '${APP_DIR}' && ./.venv/bin/python -m scripts.reencode_embeddings --model '${MODEL}' --yes" \
  > "${LOG}" 2>&1
ec=$?
echo "REENCODE_EXIT=${ec}" | tee -a "${LOG}"

if [ "${ec}" -eq 0 ]; then
    echo "[$(date)] réencodage OK -> restart ${SERVICE}" | tee -a "${LOG}"
    systemctl restart "${SERVICE}"
    sleep 4
    echo "RESTARTED is-active=$(systemctl is-active ${SERVICE})" | tee -a "${LOG}"
    echo -n "HEALTH=" | tee -a "${LOG}"
    curl -s localhost:5001/health | tee -a "${LOG}"
    echo | tee -a "${LOG}"
else
    echo "[$(date)] ÉCHEC réencodage (code ${ec}) — service NON redémarré, index inchangés." | tee -a "${LOG}"
fi
