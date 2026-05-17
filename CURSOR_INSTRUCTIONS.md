# Instructions Cursor — Quiz Certification Odoo

## Chemin du projet
```
/Users/patri/odoo-quiz/
```
Ouvrir dans Cursor : **File → Open Folder → /Users/patri/odoo-quiz**

---

## Contexte du projet

Application web Flask d'entraînement à la **certification Odoo** (examen officiel).
- 664 questions extraites depuis l'instance Odoo `senedoo.odoo.com`
- Scoring : +1 bonne réponse / -1 mauvaise / 0 si passée
- Chronomètre visible
- Questions en anglais avec traduction FR toggle
- Explications générées par Claude (CLI local, abonnement Max — PAS d'API key Anthropic)
- Bouton "Question libre" pour interroger Claude sur n'importe quel sujet Odoo
- Bouton "Recommencer" accessible en permanence dans le header

---

## Structure des fichiers

```
/Users/patri/odoo-quiz/
├── app.py                      # App Flask principale (tout-en-un : routes + HTML inline)
├── config.json                 # Config réelle (ne pas committer — contient clé API Odoo)
├── config.example.json         # Modèle de config sans secrets
├── extract_odoo.py             # Extrait les questions depuis Odoo via XML-RPC
├── generate_explanations.py    # Génère traductions FR + explications Claude (8 workers parallèles)
├── evaluate_claude.py          # Évalue Claude à l'aveugle sur les 664 questions
├── run.sh                      # Lance tout + ouvre le navigateur automatiquement
├── questions.json              # Les 664 questions avec réponses, traductions, explications (1.3 MB)
└── .venv/                      # Virtualenv Python (flask installé)
```

**Sur le bureau :**
```
/Users/patri/Desktop/Quiz Odoo.command   # Double-clic pour lancer l'app
```

---

## Lancer l'application

### Option 1 — Double-clic (recommandé)
Double-clic sur **"Quiz Odoo"** dans le Finder sur le bureau.
Ouvre un terminal + lance Flask + ouvre le navigateur sur l’URL définie par `flask.host` / `flask.port` dans `config.json` (souvent `http://127.0.0.1:5001` — le port 5001 évite le conflit macOS / AirPlay sur 5000).

### Option 2 — Terminal
```bash
cd /Users/patri/odoo-quiz
bash run.sh
```

### Option 3 — Direct
```bash
cd /Users/patri/odoo-quiz
source .venv/bin/activate
python3 app.py
# Puis ouvrir http://127.0.0.1:<flask.port> dans le navigateur (voir config.json)
```

---

## Config (`config.json`)

```json
{
  "odoo": {
    "url": "https://senedoo.odoo.com",
    "db": "senedoo",
    "login": "patrice@senedoo.com",
    "api_key": "1411f08752b62d06b769a0088f641c9385b1b5ad"
  },
  "anthropic": {
    "api_key": "ANTHROPIC_API_KEY_ICI"   ← PAS utilisé (explications pré-générées)
  },
  "survey_name": "certification odoo",
  "questions_file": "questions.json",
  "flask": { "host": "127.0.0.1", "port": 5001, "debug": false }
}
```

> ⚠️ La clé `anthropic.api_key` n'est PAS utilisée dans l'app. Les explications sont
> pré-générées dans questions.json. Le bouton "Question libre" utilise le CLI `claude`
> en subprocess (requiert Claude Code installé et authentifié).

---

## Structure de `questions.json`

```json
{
  "survey": "Certification Odoo",
  "questions": [
    {
      "id": 239,
      "title": "If you create a new sub-article...",   ← Question en anglais (original Odoo)
      "title_fr": "Si vous créez un nouvel article enfant...",  ← Traduit par Claude
      "type": "simple_choice",                          ← Type Odoo : simple_choice | multiple_choice | text_box
      "is_scored": true,
      "explication_senedoo": "",                        ← Toujours vide (description absente dans Odoo)
      "explication_claude": "Dans Odoo Knowledge...",   ← Généré par generate_explanations.py
      "answers": [
        {
          "id": 1045,
          "value": "The same users as the parent article",  ← Réponse en anglais
          "value_fr": "Les mêmes utilisateurs que l'article parent",  ← Traduit par Claude
          "is_correct": true,     ← Bonne réponse (depuis Odoo ou déterminée par Claude)
          "score": 0
        },
        ...
      ]
    }
  ]
}
```

**État actuel des données (mai 2026) :**
- 664/664 questions avec bonne réponse identifiée
- 664/664 questions avec `title_fr`
- 664/664 questions avec `explication_claude`
- 0/664 `explication_senedoo` (champ description vide côté Odoo — normal)

---

## Architecture de `app.py`

Le fichier est un seul fichier Python avec le HTML/CSS/JS en template string inline.
C'est un choix délibéré pour avoir un seul fichier à maintenir.

### Structure du fichier
```
app.py
├── Imports + config Flask
├── load_config() / load_questions()   ← chargement au démarrage
├── HTML = """..."""                    ← template complet (CSS + HTML + JS)
│   ├── <style>                        ← tous les styles
│   ├── Modal "Question à Claude"      ← overlay avec textarea
│   ├── <header>                       ← titre + boutons ↺ Recommencer + 💬 Question
│   ├── #score-bar                     ← ✔/✘/— + score total
│   ├── #start-screen                  ← écran d'accueil avec sélecteur nb questions
│   ├── #question-card                 ← carte question (progress, texte, réponses, actions, explications)
│   ├── #results                       ← écran de fin avec score final
│   └── <script>                       ← toute la logique JS
├── Routes Flask
│   ├── GET  /              → index()         ← sert le HTML
│   ├── POST /api/ask       → api_ask()       ← question libre → claude -p subprocess
│   ├── GET  /api/questions → api_questions() ← retourne N questions aléatoires
└── if __name__ == "__main__": app.run(...)
```

### Logique JS principale
```javascript
// État global
let questions = []    // questions du quiz en cours
let current = 0       // index question courante
let answered = false  // true dès qu'on a répondu ou passé
let showFr = false    // true si toggle FR activé
let score, good, bad, skip  // compteurs

// Flux principal
startQuiz()        → GET /api/questions?n=N → showQuestion()
selectAnswer(i)    → colorie les boutons → revealAnswer()
skipQuestion()     → colorie en jaune → revealAnswer()
revealAnswer()     → cache "Passer", montre "Suivante", affiche explication auto
nextQuestion()     → current++ → showQuestion()
showResults()      → affiche score final

// Modal
openModal()        → affiche le div #modal-overlay
askClaude()        → POST /api/ask → affiche réponse dans #modal-answer

// Toggle FR/EN
toggleLang()       → toggle showFr → met à jour #question-text-fr + .val-fr spans
```

---

## Scripts utilitaires

### Re-extraire les questions depuis Odoo
```bash
cd /Users/patri/odoo-quiz
source .venv/bin/activate
python3 extract_odoo.py
```
Écrase `questions.json`. Relancer `generate_explanations.py` ensuite si nouvelles questions.

### Régénérer traductions + explications manquantes
```bash
python3 generate_explanations.py
```
- 8 workers parallèles (~10 min pour 664 questions)
- **Incrémental** : ne retouche pas les questions déjà traitées (`explication_claude` non vide)
- Sauvegarde automatique toutes les 20 questions
- Si interrompu : relancer, reprend où ça s'est arrêté
- Nécessite le CLI `claude` installé et authentifié (Claude Code)

### Évaluation aveugle de Claude
```bash
python3 evaluate_claude.py
```
- Pose chaque question à Claude SANS lui dire la bonne réponse
- Compare sa réponse à `is_correct` dans le fichier
- Résultats dans `evaluation.json` + rapport terminal
- Incrémental aussi : relancer pour compléter

---

## Bugs connus et fixes déjà appliqués

### Bug données Odoo (question 282) — CORRIGÉ
La question "In Odoo Knowledge, can a record function as both a folder and an article simultaneously?"
contenait des réponses d'une autre question (réponses sur les factures).
**Fix appliqué** : réponses parasites supprimées, bonne réponse réévaluée par Claude → **True** est correct.

### Champ `is_correct` absent dans Odoo
Certaines questions n'avaient pas `is_correct` marqué dans l'API Odoo.
**Fix appliqué** : `generate_explanations.py` détermine la bonne réponse via Claude pour ces cas.
Format du prompt : `BONNE_REPONSE: <numéro>` puis `EXPLICATION: ...`

---

## Dépendances

```
flask          ← installé dans .venv
claude (CLI)   ← /opt/homebrew/bin/claude (Claude Code 2.1.114, abonnement Max)
python3        ← /Library/Developer/CommandLineTools (3.9)
```

Pas de `requirements.txt` — le `.venv` est déjà créé et `flask` installé.
Pour recréer : `python3 -m venv .venv && source .venv/bin/activate && pip install flask`

---

## Évolutions possibles (non implémentées)

1. **Mode révision** : revoir uniquement les questions ratées lors de la session précédente
2. **Statistiques par module Odoo** : regrouper questions par thème (Knowledge, Inventory, Sales...)
3. **Évaluation `evaluate_claude.py`** : pas encore lancée (0 questions évaluées dans `evaluation.json`)
4. **`explication_senedoo`** : toujours vide — si tu trouves une source d'explications Udemy/Senedoo,
   les injecter dans ce champ pour avoir la comparaison Senedoo vs Claude dans l'UI
5. **Persistance des sessions** : le score repart à 0 à chaque rechargement, pas d'historique

---

## Notes importantes pour Cursor

- **Ne pas modifier `questions.json` à la main** — 1.3 MB, risque de corruption JSON
- **Le HTML est inline dans `app.py`** (variable `HTML = """..."""`) — c'est voulu
- **Pas d'API Anthropic** — tout passe par subprocess `claude -p "..."` (CLI local)
- **Redémarrer Flask** après chaque modification de `app.py` (pas de hot-reload)
- **Le `.venv` est déjà prêt** — toujours `source .venv/bin/activate` avant `pip install`
- **`config.json` contient des secrets** — ne pas committer, ne pas afficher

---

*Généré le 14 mai 2026 — Session Claude Code (claude-sonnet-4-6)*
