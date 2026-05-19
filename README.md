# odoo-quiz

Application locale d'entraînement à la certification Odoo (v18 et v19).

## Documentation Odoo (RAG)

Index vectoriel des pages officielles dans `data/odoo_docs.sqlite`. Chaque chunk est tagué **`version`** (`18.0` ou `19.0`) pour ne pas mélanger les docs au moment du RAG.

### Prérequis

```bash
source .venv/bin/activate
pip install -r requirements.txt
python3 scripts/migrate_schema.py   # une fois (chunks.version + questions.target_version)
```

### Valider la couverture (dry-run)

```bash
python3 -m scripts.ingest_odoo_docs --version 19.0 --section applications --dry-run
python3 -m scripts.ingest_odoo_docs --version 18.0 --section applications --dry-run
```

Sections : `applications` (défaut), `developer`, `administration`, `all`.

Source d’URLs : `https://www.odoo.com/documentation/<version>/sitemap.xml` ; si 404, repli sur `searchindex.js`.

### Ingestion

**Odoo 19** (doc fonctionnelle) :

```bash
python3 -m scripts.ingest_odoo_docs --version 19.0 --section applications
```

**Odoo 18** (défaut si `--version` omis) :

```bash
python3 -m scripts.ingest_odoo_docs
# équivalent :
python3 -m scripts.ingest_odoo_docs --version 18.0 --section applications
```

Test sur 50 pages avant un crawl complet :

```bash
python3 -m scripts.ingest_odoo_docs --version 19.0 --section applications --limit 50
```

- Throttle **1 req/s**, pas de limite de pages (sauf `--limit` explicite).
- `chunk_id` préfixé par la version (ex. `19.0__applications_finance_accounting__chunk_0`) — pas de collision v18/v19.
- Script **idempotent** par version : re-run met à jour, ne duplique pas.

### Classification des questions (v18 / v19 / both)

Tague automatiquement `target_version` sur `questions.json` via Claude + top-3 chunks doc **v18** et **v19** (requêtes séparées).

```bash
# Valider sur 20 questions (sans écrire)
python3 -m scripts.classify_versions --reclassify-all --dry-run --limit 20

# Une question
python3 -m scripts.classify_versions --question-id 325 --dry-run

# Toute la banque (confiance haute → JSON ; sinon → data/classification_review.csv)
python3 -m scripts.classify_versions --reclassify-all
```

- Par défaut : seules les questions avec `target_version` **NULL** ou vide (après migration tout est en `18.0` → utiliser `--reclassify-all` la première fois).
- Parallélisme : **5** appels simultanés max.
- Sauvegarde automatique de `questions.json` avant écriture.

### Revue des suggestions peu fiables

```bash
python3 -m app.review --confidence basse
python3 -m app.review --version 19.0 --confidence basse
```

### Revue classification manuelle

Fichier généré par le script : `data/classification_review.csv` (questions à confiance moyenne/basse ou erreur API).

Journal suggestions : `data/suggestions.log` (JSONL).
