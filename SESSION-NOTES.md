# Notes de passation — état après session du 19 mai 2026

Ce fichier complète `BRIEFING_COWORK_quiz_odoo.md.docx`. Il ne le remplace pas :
le briefing reste la source d'origine pour les décisions architecturales. Ce
document liste les **écarts**, **décisions prises pendant l'exécution**, et la
**dette technique** identifiée.

---

## État courant des phases

| Phase | État | Détail |
|---|---|---|
| 1 — Déploiement VPS | ✅ | https://quiz-odoo.picvert-senedoo.org/ — HTTPS, gunicorn `odoo-quiz.service`, Caddy mutualisé avec ResourceSpace |
| 2 — Couverture v19 inventory | ✅ | 233/232 chunks v18/v19 (parité) |
| 3 — Scope 3 tiers | ✅ | `app/study_modules.py`, 39 modules, validateur de chemins (0 chemin à corriger) |
| 4 — Pipeline images | 🟡 **run en background** | Tables `doc_images` + `chunk_images`, code livré. Le run de re-ingestion images tourne sur le VPS via `nohup`. Au dernier check 23:34 : 733 pages traitées / 1719, 2555 doc_images. PID : `cat /tmp/reingest.pid` sur le VPS. |
| 5.1 schéma question étendu | ✅ | `app/question_schema.py` |
| 5.2 calibrage Udemy | ✅ | `data/calibration_report.md` |
| 5.3 plan génération | ✅ | `data/generation_plan.json` — 3000 questions atteignables, 0 overflow |
| 5.4 mini-run 50 q | ✅ | 88 % validation, qualité humaine excellente (cf. inspect 8 q dans le chat) |
| Optims génération | ✅ | mode `--async` + prompt caching ajoutés |
| **5.5 full run + judge** | **⏳ à faire** | Voir spec détaillée plus bas |
| 5.6 insertion atomique | ⏳ | Doit utiliser `_save_questions_file_raw()` |
| 5.7 embeddings nouvelles q | ⏳ | Append à `bank_embeddings.npz` |
| 5.8 invalidate cache | ⏳ | `bank_embeddings.invalidate_embedding_cache()` |
| 6 traduction FR Udemy | ⏳ | Briefing dit ~$2 via Batch API |
| 7 UX filtres / review | ⏳ | Filtres source/tier/version/status, bouton Signaler, page admin |

---

## Écarts notables par rapport au briefing

1. **Caddy au lieu de nginx** (Phase 1.7) : le VPS tourne déjà Caddy pour ResourceSpace. On a ajouté un block `quiz-odoo.picvert-senedoo.org` au `Caddyfile` existant plutôt que d'installer nginx en parallèle. **HTTPS auto via Let's Encrypt fonctionne.**

2. **Sous-domaine `quiz-odoo.picvert-senedoo.org`** (et non `quiz.<...>` comme suggéré). Choix Patrice.

3. **`app/study_modules.py` au lieu de `app/config.py`** (Phase 3.1) : `config.py` était focalisé sur l'état runtime (target_certification). On a créé un nouveau fichier dédié pour la source de vérité des modules — séparation des concerns plus propre. Helpers : `all_modules()`, `tier_of(m)`, `is_v19_only(m)`, `url_paths_for(m)`, `modules_in_tier(tier)`, constantes `STUDY_MODULES`, `TIER_BUDGET`, `V19_ONLY_MODULES`, `MODULE_URL_PATHS`, `ALL_TIERS`.

4. **WSGI shim `wsgi.py`** pour contourner la collision Python entre `app.py` (fichier Flask) et `app/` (package). Gunicorn lance `wsgi:app` qui charge `app.py` par chemin de fichier explicite. Le code local (Cursor / `python app.py`) continue de marcher inchangé.

5. **`/api/ask` patché** : passait par `subprocess(["claude", "-p", ...])` (CLI Claude Code, non dispo sur VPS) → bascule sur le SDK Anthropic Python via les helpers `app.llm._anthropic_key()`, `_answer_model()`, `extract_text_from_content()`. Cohérent avec `/api/suggest-answer`.

6. **Anomalies SQL `audit_doc_coverage`** : `MIN_MODULE_CHUNKS = 150` est inadapté pour les modules à très peu d'URLs sitemap (`productivity/whatsapp` : 1 URL). Le rapport flag des "sous-représentés" qui sont en réalité des plafonds naturels. **Pas corrigé** — c'est de l'info, pas du bloquant. À rendre tier-aware si on retouche le validateur.

7. **Bridge `cmdbridge.sh`** : timeout par défaut passé de 300 s → **1800 s** (cf. commit modifié). Permet override par `# TIMEOUT=N` dans le `.req`. Idéal pour les commandes Claude API longues ou les rsync gros volume.

---

## Anomalies / dette technique notée

- **28 questions Udemy hors normes** (option count = 2 ou 5) — vs briefing "3 ou 4". À nettoyer un jour.
- **2 questions Udemy avec ≠1 bonne réponse** (devrait être exactement 1). À nettoyer.
- **`productivity/knowledge`** marqué v19-only mais a 1 URL en v18 (1 chunk en base). Annomalie cosmétique.
- **Alt-text images Odoo** : Sphinx émet le chemin (`'../../../../_images/foo.png'`) par défaut. Pas un alt-text descriptif. Pour la génération (Phase 5.5), on peut compenser via titre+section du chunk + texte autour.
- **`gunicorn` log warning** : `[ERROR] Control server error: Read-only file system: '/home/senedoo/.gunicorn'`. Dû à `ProtectHome=read-only` dans le service systemd. Cosmétique — service fonctionne. À fixer en relocalisant le control socket (ex. `/opt/odoo-quiz/run/`).
- **Sitemap.xml Odoo 18/19 retourne 404** : `ingest_odoo_docs.py` a un fallback `searchindex.js` qui marche. Les rapports `sitemap_vs_ingestion.md` indiquent bien "searchindex.js" comme source.
- **Anthropic prompt caching < 1024 tokens** : actuellement, le `SYSTEM_PROMPT` de `generate_questions.py` fait ~600 tokens, donc le caching est silencieusement ignoré. Si on renforce le system avec des consignes plus détaillées (ou un exemple complet inline), on franchit le seuil et on gagne -90 % input cached.

---

## Spec détaillée pour Phase 5.5 (à coder en nouvelle session)

### 5.5.a — Orchestrateur multi-modules

Nouveau script `scripts/run_full_generation.py` qui :

1. Charge `data/generation_plan.json`.
2. Pour chaque `(tier, module, version)` avec `target_total > 0` :
   - Appelle `generate_questions(module, version, count=target_total, --async --concurrency 20)`.
   - Stocke chaque batch dans `data/generated_pending/<module>-v<ver>-<batch_id>.jsonl`.
3. **Mode reprise** : `--resume-from <batch_id>` qui skip les (module, version) déjà traités.
4. **Mode dry-run** : affiche le plan d'exécution + estimation coût + temps.

Estimations cible :
- ~750 appels Claude × ~20 s = 15 000 s en sync = 4h
- En async concurrency 20 → ~30-60 min
- Avec Batch API → ~50 min en file d'attente + traitement (-50 % coût)

### 5.5.b — Pipeline judge

Nouveau script `scripts/judge_questions.py` :

1. Pour chaque fichier `data/generated_pending/*.jsonl` :
   - Pour chaque question : appel Claude (Sonnet 4.6) avec un prompt qui demande de noter sur **5 critères** (1-5) :
     - factualité
     - clarté
     - distracteurs (plausibles, non triviaux, pas plus courts que la bonne)
     - niveau cert (fonctionnel, pas technique dev)
     - pertinence module
2. Score final = **MIN des 5 critères** (maillon faible).
3. Décision :
   - `score ≥ 4` → `accept` → `status: "verified_by_judge"`
   - `score == 3` → `review` → `status: "unverified"`
   - `score ≤ 2` → `reject` → log dans `data/rejected_questions.jsonl`, NE PAS insérer.
4. Mise à jour des champs `judge_score`, `judge_decision`, `judge_reasons`, `status` directement dans le pending JSONL.

Mode async + semaphore=20 idem que le générateur. Batch API si pertinent (briefing recommande).

### 5.5.c — Dédup vectorisée

Nouveau utilitaire dans `bank_embeddings.py` ou `scripts/dedupe_pending.py` :

1. Pour les questions pending acceptées, calculer leur embedding via `bank_embeddings.embed_texts([title for q in pending])`.
2. Matrice similarité cosinus contre toutes les questions existantes dans `bank_embeddings.npz`.
3. Si max sim > **0.92** → drop la pending (doublon avec une question existante).
4. Et entre les pendings elles-mêmes (matrice triangulaire), drop les paires > 0.92, garder la première.

Briefing : `duplicate_score_threshold: 0.98` (config) — mais aussi mentionne 0.92 pour le pipeline génération. À clarifier ; je propose **0.92** comme un seuil intermédiaire.

### 5.5.d — Few-shot rotatif par module

À ajouter dans `generate_questions.py` (override de `pick_few_shot`) :

Au lieu de tirer 3 questions Udemy au hasard, filtrer par **module inféré** (via RAG sur `title` → top chunk → module). Cache l'inférence dans un fichier `data/udemy_modules.json` après une passe unique pour économiser des appels embedding.

Sans ça, le few-shot reste générique et peut détonner avec le module en cours.

---

## Spec détaillée pour Phase 5.6 (insertion atomique)

Nouveau script `scripts/insert_pending_questions.py` :

1. Lit toutes les questions pending acceptées (status `verified_by_judge` ou `unverified`).
2. Sanity check **avant écriture** :
   - `validate_generated_question(q)` retourne `[]` pour CHAQUE q.
   - Aucun `id` déjà présent dans la banque (collision).
   - Aucun `answer.id` déjà présent (collision globale).
3. Backup horodaté de `questions.json` (briefing rule de pilotage).
4. `_save_questions_file_raw(questions_data)` (vu dans `app.py`, à importer).
5. `bank_embeddings.invalidate_embedding_cache()` (Phase 5.8).
6. Logue dans `data/insertion_log.jsonl` (qid, batch_id, timestamp).

**JAMAIS** d'écriture directe dans `questions.json` sans passer par `_save_questions_file_raw()` — il fait l'écriture atomique (rename) + invalidation cache.

---

## Spec Phase 5.7 — embeddings

Une fois les questions insérées :

```python
from bank_embeddings import embed_texts, save_npz, load_npz_meta
new_titles = [q["title"] for q in inserted]
new_vecs = embed_texts(new_titles, batch_size=128)  # briefing : 128 textes/appel
# Append au npz existant + meta
save_npz(append=True, vectors=new_vecs, qids=[q["id"] for q in inserted])
```

À vérifier l'API exacte de `bank_embeddings.py` au moment de coder.

---

## Bridge `cmdbridge.sh` — usage

Le bridge tourne dans un terminal sur le Mac de Patrice. **Tant qu'il n'est
pas Ctrl+C**, une nouvelle session Cowork peut lui envoyer des `.req` et
récupérer les `.out`.

```bash
# Démarrer (si fenêtre fermée)
bash ~/odoo-quiz/cmdbridge.sh

# Convention de nommage
~/odoo-quiz/.bridge/NNN-description.req → NNN-description.out

# Override timeout (sec) dans le .req
# TIMEOUT=300
```

Au 19 mai 23h35, le dernier `.req` traité était `046`. Le prochain à utiliser
est **`047`**.

---

## Accès SSH

Cf. `BRIEFING` (rappel) :
```bash
ssh -i ~/.ssh/niokolo_claude senedoo@picvert-senedoo.org   # app quiz
ssh -i ~/.ssh/niokolo_claude niokolo@picvert-senedoo.org   # admin (sudo)
```

User OS : `senedoo` (uid 1001) — créé pendant la session, isolé de `niokolo`
(qui possède ResourceSpace).

---

## Quick commands pour la nouvelle session

```bash
# 1. Vérifier l'état du run images en background
ssh -i ~/.ssh/niokolo_claude senedoo@picvert-senedoo.org \
  "ps -ef | grep [r]eingest; tail -10 /opt/odoo-quiz/logs/reingest_images_v2.log"

# 2. Stats DB courantes
ssh -i ~/.ssh/niokolo_claude senedoo@picvert-senedoo.org \
  "cd /opt/odoo-quiz && ./.venv/bin/python -c '
import sqlite3
from app.odoo_docs_rag import db_path
c = sqlite3.connect(db_path())
print(\"chunks:\", c.execute(\"SELECT COUNT(*) FROM chunks\").fetchone()[0])
print(\"doc_images:\", c.execute(\"SELECT COUNT(*) FROM doc_images\").fetchone()[0])
print(\"chunk_images:\", c.execute(\"SELECT COUNT(*) FROM chunk_images\").fetchone()[0])
'"

# 3. Voir les pending
ls -la ~/odoo-quiz/data/generated_pending/ 2>/dev/null
ssh -i ~/.ssh/niokolo_claude senedoo@picvert-senedoo.org \
  "ls -la /opt/odoo-quiz/data/generated_pending/"

# 4. App health
curl -s https://quiz-odoo.picvert-senedoo.org/health
```

---

*Document généré à la fin de la session du 19 mai 2026. Mettre à jour à chaque session future.*
