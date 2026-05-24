#!/usr/bin/env python3
"""
App Flask de quiz Odoo.
Scoring : +1 bonne réponse / -1 mauvaise réponse / 0 sans réponse.
Explications pré-générées stockées dans questions.json.
"""

import copy
import html
import json
import os
import random
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

import shutil
import uuid

import generate_explanations as ge
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    stream_with_context,
    url_for,
)
from werkzeug.utils import secure_filename

from import_preview_enrich import (
    bank_capture_merge_context,
    bank_identical_meta,
    enrich_item_for_preview,
)
from import_screenshot import CaptureNoQuizContentError, extract_items_from_capture
from import_udemy import (
    apply_capture_items_to_bank,
    manual_vision_fallback_udemy_item,
    title_requires_capture_image,
    title_suggests_balance_context,
    validate_udemy_item,
)
from extract_odoo import OdooExtractError, extract_survey_to_file, list_surveys, load_config as load_odoo_extract_config
from question_images import (
    has_valid_question_image,
    normalize_question_media_rel,
    preview_region_data_url,
)
from quiz_llm import api_available, parse_json_value, run_prompt_with_images

CONFIG_FILE = Path(__file__).parent / "config.json"
# Incrémenter à chaque livraison (affichée dans l’UI : en-tête, onglet, pied de page ; F5 si auto_reload).
APP_VERSION = "2.5.0"
app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 Mo (captures)


@app.after_request
def _no_store_html(resp):
    """Évite un HTML obsolète (ex. ancienne v affichée) si le navigateur ou un proxy met en cache."""
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        if request.path.startswith("/import-capture/preview"):
            resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp

# ---------------------------------------------------------------------------
# Config & données
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)

def load_questions(cfg):
    qfile = Path(__file__).parent / cfg.get("questions_file", "questions.json")
    if not qfile.exists():
        return []
    with open(qfile, encoding="utf-8") as f:
        data = json.load(f)
    out = data.get("questions", [])
    if not isinstance(out, list):
        return []
    for q in out:
        if isinstance(q, dict) and (q.get("question_image") or "question_image" in q):
            q["question_image"] = normalize_question_media_rel(q.get("question_image"))
    return out

CFG = load_config()
ALL_QUESTIONS = load_questions(CFG)

try:
    from app.settings_db import init_settings_db

    init_settings_db()
except Exception:
    pass

try:
    from bank_embeddings import schedule_bank_embedding_warmup

    schedule_bank_embedding_warmup(ALL_QUESTIONS)
except Exception:
    pass


def reload_questions():
    global ALL_QUESTIONS
    ALL_QUESTIONS = load_questions(CFG)
    try:
        from bank_embeddings import clear_bank_vector_index, schedule_bank_embedding_warmup

        clear_bank_vector_index()
        schedule_bank_embedding_warmup(ALL_QUESTIONS)
    except Exception:
        pass


_DEFAULT_TOPIC = "Général / Odoo"
_TOPIC_RULES = (
    ("Knowledge", ("knowledge", "article", "workspace", "sous-article", "permission")),
    ("Survey", ("survey", "sondage", "questionnaire", "respondent", "certifié")),
    ("Site web / eCommerce", ("website", "snippet", "theme", "visitor", "ecommerce", "boutique")),
    ("CRM", ("crm", "opportunity", "pipeline", "lead", "prospect")),
    ("Ventes", ("sale order", "sales order", "quotation", "devis", "commande client")),
    ("Achats", ("purchase order", "rfq", "fournisseur")),
    ("Stock / Inventaire", ("inventory", "stock", "warehouse", "inventaire", "entrepôt")),
    ("Fabrication (MRP)", ("manufacturing", "bom", "work order", "mrp", "fabrication")),
    ("Comptabilité", ("accounting", "invoice", "journal", "comptabilité", "facture")),
    ("Projet", ("project", "task", "milestone", "tâche", "projet")),
    ("RH / Employés", ("employee", "leave", "time off", "congé", "employé")),
    ("Point de vente", ("point of sale", "pos ", "caisse")),
    ("Helpdesk", ("helpdesk", "ticket", "sla")),
    ("Documents", ("documents", "document", "dms")),
    ("Discuss / Email", ("discuss", "mail gateway", "chatter")),
)


def _infer_odoo_topic(q: dict) -> str:
    parts = [
        q.get("title") or "",
        q.get("title_fr") or "",
        (q.get("explication_claude") or "")[:800],
        (q.get("explication_senedoo") or "")[:800],
    ]
    for a in q.get("answers") or []:
        parts.append(a.get("value") or "")
        parts.append(a.get("value_fr") or "")
    blob = " ".join(parts).lower()
    best, score = _DEFAULT_TOPIC, 0
    for name, keys in _TOPIC_RULES:
        s = sum(1 for k in keys if k in blob)
        if s > score:
            score, best = s, name
    return best


def _sort_questions_bank(questions: list) -> list:
    def key(q):
        qid = q.get("id")
        if isinstance(qid, (int, float)):
            return (0, int(qid))
        try:
            return (0, int(qid))
        except (TypeError, ValueError):
            return (1, 0)

    return sorted(questions, key=key)


def _display_topic(q: dict) -> tuple:
    stored = (q.get("topic") or "").strip()
    if stored:
        return stored, False
    return _infer_odoo_topic(q), True


def _max_answer_id(questions: list) -> int:
    m = 0
    for q in questions:
        for a in q.get("answers") or []:
            try:
                m = max(m, int(a.get("id", 0)))
            except (TypeError, ValueError):
                pass
    return m


def _find_question_index(data: dict, q_id: int):
    qs = data.get("questions")
    if not isinstance(qs, list):
        return None
    for i, q in enumerate(qs):
        try:
            if int(q.get("id")) == q_id:
                return i
        except (TypeError, ValueError):
            continue
    return None


def _parse_question_id_param(raw: str) -> Optional[int]:
    """Parse id URL (positif ou négatif — ex. sondages Senedoo id -1…-6)."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _correct_1based_from_answers(answers: Optional[list]) -> Optional[int]:
    for j, a in enumerate(answers or []):
        if a.get("is_correct"):
            return j + 1
    return None


def _normalized_answer_source(q: dict) -> Optional[str]:
    src = q.get("correct_answer_source")
    if src in ("udemy", "odoo", "claude", "user"):
        return src
    if any(a.get("is_correct") for a in (q.get("answers") or [])):
        return "udemy"
    return None


def _bank_put_answers(old_q: dict, rows: list, global_max_aid: int) -> list:
    old_by_id = {}
    for a in old_q.get("answers") or []:
        try:
            old_by_id[int(a["id"])] = a
        except (TypeError, ValueError, KeyError):
            continue
    next_aid = global_max_aid
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Format de réponse invalide.")
        val = (row.get("value") or "").strip()
        if not val:
            raise ValueError("Chaque réponse doit avoir un texte (EN) non vide.")
        vf = (row.get("value_fr") or "").strip()
        is_ok = bool(row.get("is_correct"))
        rid = row.get("id")
        oid = None
        if rid is not None:
            try:
                rid_i = int(rid)
                if rid_i in old_by_id:
                    oid = rid_i
            except (TypeError, ValueError):
                pass
        if oid is None:
            next_aid += 1
            oid = next_aid
        next_aid = max(next_aid, oid)
        out.append(
            {
                "id": oid,
                "value": val,
                "value_fr": vf,
                "is_correct": is_ok,
                "score": 0.0,
            }
        )
    if len(out) < 2:
        raise ValueError("Au moins deux réponses sont requises.")
    if sum(1 for a in out if a["is_correct"]) != 1:
        raise ValueError("Exactement une réponse doit être marquée comme correcte.")
    return out


def _load_questions_file_raw():
    path = Path(__file__).parent / CFG.get("questions_file", "questions.json")
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_questions_file_raw(data: dict) -> None:
    path = Path(__file__).parent / CFG.get("questions_file", "questions.json")
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
    try:
        from bank_embeddings import invalidate_embedding_cache

        invalidate_embedding_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quiz Odoo — v{{ app_version }}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f0f4ff; color: #1a1a2e; min-height: 100vh; }
  header { background: #714B67; color: white; padding: 1rem 2rem;
           display: flex; align-items: center; justify-content: space-between;
           flex-wrap: wrap; gap: .5rem .75rem; width: 100%; }
  header .title-block { display: flex; flex-direction: column; align-items: flex-start; gap: .1rem;
                         flex: 0 1 auto; min-width: 0; }
  .header-nav {
    flex: 1 1 16rem; min-width: 0; width: 100%; max-width: 100%;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 6.25rem), 1fr));
    gap: .35rem; align-items: stretch;
  }
  header h1 { font-size: 1.2rem; }
  .app-version { font-size: .78rem; font-weight: 600; opacity: .92; letter-spacing: .04em; }
  #timer { font-size: 1.5rem; font-weight: bold; font-variant-numeric: tabular-nums; }
  #score-bar { background: white; padding: .6rem 2rem; font-size: .9rem;
               border-bottom: 1px solid #ddd; display: flex; gap: 2rem; }
  .score-item { font-weight: 600; }
  .good { color: #16a34a; } .bad { color: #dc2626; } .skip { color: #64748b; }
  main { max-width: 860px; margin: 2rem auto; padding: 0 1rem; }
  #question-card { background: white; border-radius: 12px; padding: 2rem;
                   box-shadow: 0 2px 8px rgba(0,0,0,.08); }
  #progress { font-size: .85rem; color: #64748b; margin-bottom: .5rem;
              display: flex; align-items: center; justify-content: space-between; }
  #question-text { font-size: 1.1rem; font-weight: 600; margin-bottom: .4rem; line-height: 1.5; }
  #question-text-fr { font-size: .95rem; color: #64748b; font-style: italic;
                      margin-bottom: 1.2rem; line-height: 1.4; display: none; }
  .question-media {
    margin: 0 0 1.15rem;
    border-radius: 10px;
    overflow: hidden;
    border: 1px solid #e2e8f0;
    background: #f8fafc;
    text-align: center;
  }
  .question-media img {
    display: block;
    width: 100%;
    max-height: min(52vh, 520px);
    height: auto;
    object-fit: contain;
    cursor: zoom-in;
    background: #fff;
  }
  .lang-toggle { font-size: .8rem; background: #f1f5f9; border: 1px solid #e2e8f0;
                 border-radius: 6px; padding: .2rem .6rem; cursor: pointer;
                 color: #475569; font-weight: 600; }
  .lang-toggle.active { background: #714B67; color: white; border-color: #714B67; }
  .progress-tools { display: flex; gap: .4rem; align-items: center; }
  .flag-btn { font-size: .8rem; background: #fff; border: 1px solid #fecaca; color: #b91c1c;
              border-radius: 6px; padding: .2rem .6rem; cursor: pointer; font-weight: 600; transition: .15s; }
  .flag-btn:hover:not(:disabled) { background: #fef2f2; }
  .flag-btn.done { background: #fee2e2; color: #7f1d1d; border-color: #fca5a5; cursor: default; }
  .flag-btn:disabled { opacity: .7; cursor: default; }
  .answer-btn { display: block; width: 100%; text-align: left; padding: .75rem 1rem;
                margin-bottom: .5rem; border: 2px solid #e2e8f0; border-radius: 8px;
                background: white; cursor: pointer; transition: .15s; }
  .answer-btn:hover:not(:disabled) { border-color: #714B67; background: #faf5ff; }
  .answer-btn.correct { border-color: #16a34a; background: #f0fdf4; }
  .answer-btn.wrong   { border-color: #dc2626; background: #fef2f2; }
  .answer-btn.missed  { border-color: #f59e0b; background: #fffbeb; }
  .val-en { font-size: .95rem; }
  .val-fr { display: none; font-size: .85rem; color: #64748b; font-style: italic; margin-top: .15rem; }
  #actions { margin-top: 1.5rem; display: flex; gap: 1rem; flex-wrap: wrap; }
  .btn { padding: .6rem 1.4rem; border-radius: 8px; border: none; cursor: pointer;
         font-size: .95rem; font-weight: 600; transition: .15s; }
  .btn-primary { background: #714B67; color: white; }
  .btn-primary:hover { background: #5a3a52; }
  .btn-secondary { background: #e2e8f0; color: #1a1a2e; }
  .btn-secondary:hover { background: #cbd5e1; }
  #explanations { margin-top: 1.5rem; display: none; gap: 1rem; flex-direction: column; }
  .expl-block { padding: 1rem; font-size: .9rem; line-height: 1.6;
                white-space: pre-wrap; border-radius: 0 8px 8px 0; }
  .expl-senedoo { background: #fff8e1; border-left: 4px solid #f59e0b; }
  .expl-claude  { background: #f0f4ff; border-left: 4px solid #714B67; }
  .expl-label { font-size: .75rem; font-weight: 700; text-transform: uppercase;
                letter-spacing: .05em; margin-bottom: .4rem; opacity: .6; }
  #results { display: none; text-align: center; padding: 2rem; }
  #results h2 { font-size: 2rem; margin-bottom: 1rem; }
  #results .final-score { font-size: 3rem; font-weight: bold; color: #714B67; }
  #results .stats { margin: 1.5rem 0; font-size: 1rem; color: #64748b; }
  #start-screen { text-align: center; padding: 3rem 1rem; }
  #start-screen h2 { font-size: 1.8rem; margin-bottom: 1rem; color: #714B67; }
  #start-screen p { color: #64748b; margin-bottom: 2rem; }
  input[type=number] { padding: .5rem; border: 2px solid #e2e8f0; border-radius: 8px;
                       width: 80px; text-align: center; font-size: 1rem; }
  /* Modal question libre */
  #modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5);
                   z-index: 100; align-items: center; justify-content: center; }
  #modal-overlay.open { display: flex; }
  #modal-box { background: white; border-radius: 12px; padding: 1.5rem; width: 90%;
               max-width: 600px; box-shadow: 0 8px 32px rgba(0,0,0,.2); }
  #modal-box h3 { font-size: 1.1rem; margin-bottom: 1rem; color: #714B67; }
  #modal-question { width: 100%; padding: .7rem; font-size: .95rem; border: 2px solid #e2e8f0;
                    border-radius: 8px; resize: vertical; min-height: 80px; font-family: inherit; }
  #modal-answer { margin-top: 1rem; padding: 1rem; background: #f0f4ff;
                  border-left: 4px solid #714B67; border-radius: 0 8px 8px 0;
                  font-size: .9rem; line-height: 1.6; white-space: pre-wrap; display: none; }
  #modal-actions { margin-top: 1rem; display: flex; gap: .75rem; justify-content: flex-end; }
  /* Boutons header */
  .header-btn { background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.3);
                color: white; border-radius: 8px; padding: .4rem .5rem; cursor: pointer;
                font-size: clamp(.72rem, 2vw, .85rem); font-weight: 600; transition: .15s;
                display: flex; align-items: center; justify-content: center;
                width: 100%; min-width: 0; text-align: center; white-space: nowrap;
                overflow: hidden; text-overflow: ellipsis; }
  .header-btn:hover { background: rgba(255,255,255,.25); }
  #timer { display: flex; align-items: center; justify-content: center; width: 100%;
           min-width: 0; font-size: clamp(.95rem, 2.5vw, 1.5rem); }
  @media (max-width: 640px) {
    header { padding: .65rem 1rem; }
    .header-nav { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    #timer { grid-column: 1 / -1; }
  }
  .app-build { text-align: center; padding: .6rem 1rem 1.25rem; font-size: .82rem; color: #94a3b8; }
  .app-build strong { color: #64748b; font-weight: 600; }
</style>
</head>
<body>
<div id="modal-overlay">
  <div id="modal-box">
    <h3>💬 Poser une question à Claude</h3>
    <textarea id="modal-question" placeholder="Ex: Comment fonctionnent les règles de rangement dans Odoo Inventory ?"></textarea>
    <div id="modal-answer"></div>
    <div id="modal-actions">
      <button class="btn btn-secondary" onclick="closeModal()">Fermer</button>
      <button class="btn btn-primary" id="modal-submit" onclick="askClaude()">Envoyer</button>
    </div>
  </div>
</div>
<header>
  <div class="title-block">
    <h1>🎓 Quiz Odoo</h1>
    <span class="app-version" title="Version de l'application">v{{ app_version }}</span>
  </div>
  <nav class="header-nav" aria-label="Navigation principale">
    <div class="cert-bar" style="display:flex;flex-wrap:wrap;align-items:center;gap:.4rem .55rem;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.28);border-radius:8px;padding:.35rem .5rem;font-size:.78rem;grid-column:1/-1">
      <label for="quiz-cert-select" style="font-weight:600;margin:0">Certif.</label>
      <select id="quiz-cert-select" style="background:#fff;color:#1a1a2e;border:none;border-radius:6px;padding:.25rem .4rem;font:inherit;font-weight:600">
        <option value="18.0"{% if target_certification == '18.0' %} selected{% endif %}>v18</option>
        <option value="19.0"{% if target_certification == '19.0' %} selected{% endif %}>v19</option>
      </select>
      <label for="quiz-module-select" style="font-weight:600;margin:0 0 0 .35rem">Module</label>
      <select id="quiz-module-select" style="background:#fff;color:#1a1a2e;border:none;border-radius:6px;padding:.25rem .4rem;font:inherit;font-weight:600;max-width:280px">
        <option value="">— Choisis un module —</option>
      </select>
      <label style="font-size:.72rem;display:flex;align-items:center;gap:.25rem;margin:0 0 0 .35rem;cursor:pointer" title="Inclure les questions générées notées 3/5 par le judge (qualité moyenne)">
        <input type="checkbox" id="quiz-include-hidden" style="margin:0">
        inclure unverified
      </label>
      <span id="quiz-cert-count">— sélectionne un module —</span>
    </div>
    <a class="header-btn" href="/" title="Accueil quiz" aria-current="page">🎓 Quiz</a>
    <a class="header-btn" href="/banque">📋 Banque</a>
    <a class="header-btn" href="/import-capture">📷 Capture</a>
    <a class="header-btn" href="/admin/review" title="Revue des questions (accès restreint)">🔧 Admin</a>
    <button type="button" class="header-btn" onclick="openModal()">💬 Question</button>
    <button type="button" class="header-btn" onclick="if(confirm('Recommencer un nouveau quiz ?')) location.reload()">↺ Recommencer</button>
    <div id="timer" title="Temps écoulé (quiz)">00:00</div>
  </nav>
</header>
<div id="score-bar" style="display:none">
  <span class="score-item good">✔ <span id="s-good">0</span></span>
  <span class="score-item bad">✘ <span id="s-bad">0</span></span>
  <span class="score-item skip">— <span id="s-skip">0</span></span>
  <span class="score-item">Score : <span id="s-total">0</span></span>
</div>
<main>
  <div id="start-screen">
    <h2>Quiz Odoo</h2>
    <p id="start-info">{{ total }} questions disponibles.<br>Scoring : +1 bonne / −1 mauvaise / 0 saut.</p>
    <div style="margin-bottom:1.5rem">
      <label>Nombre de questions : <input type="number" id="q-count" value="20" min="1" max="{{ total }}"></label>
    </div>
    <button class="btn btn-primary" onclick="startQuiz()">Commencer</button>
    <p style="margin-top:1.5rem;font-size:.9rem">
      <a href="/banque" style="color:#714B67;font-weight:600">📋 Banque de questions</a>
      <span style="color:#64748b"> — consulter ou modifier les entrées de <code>questions.json</code></span><br>
      <a href="/import-capture" style="color:#714B67;font-weight:600">📷 Capture quiz (Udemy / Odoo)</a>
      <span style="color:#64748b"> — importer une question depuis une capture (API Anthropic)</span>
    </p>
  </div>

  <div id="question-card" style="display:none">
    <div id="progress">
      <span id="progress-text"></span>
      <div class="progress-tools">
        <button class="flag-btn" id="btn-flag" onclick="flagQuestion()" title="Signaler une question erronée (elle sera retirée du quiz)">⚐ Signaler</button>
        <button class="lang-toggle" id="lang-btn" onclick="toggleLang()" title="Afficher la traduction française">🇫🇷 FR</button>
      </div>
    </div>
    <div id="question-text"></div>
    <div id="question-text-fr"></div>
    <div id="question-media" class="question-media" style="display:none" role="img" aria-label="Illustration de la question">
      <img id="question-image" src="" alt="" loading="lazy">
    </div>
    <div id="answers"></div>
    <div id="actions">
      <button class="btn btn-secondary" id="btn-skip" onclick="skipQuestion()">Passer →</button>
      <button class="btn btn-primary" id="btn-next" onclick="nextQuestion()" style="display:none">Question suivante →</button>
    </div>
    <div id="explanations" style="display:none">
      <div id="expl-senedoo" class="expl-block expl-senedoo" style="display:none">
        <div class="expl-label">📚 Senedoo / Udemy</div>
        <div id="expl-senedoo-text"></div>
      </div>
      <div id="expl-claude" class="expl-block expl-claude" style="display:none">
        <div class="expl-label">🤖 Claude</div>
        <div id="expl-claude-text"></div>
      </div>
    </div>
  </div>

  <div id="results">
    <h2>Quiz terminé !</h2>
    <div class="final-score" id="final-score"></div>
    <div class="stats" id="final-stats"></div>
    <button class="btn btn-primary" onclick="location.reload()">Recommencer</button>
  </div>
</main>
<footer class="app-build">Version livrée : <strong>v{{ app_version }}</strong></footer>

<script>
let questions = [], current = 0, answered = false;
let score = 0, good = 0, bad = 0, skip = 0;
let startTime, timerInterval;
let showFr = false;
let total = {{ total }};
const withClaude  = {{ with_claude }};
const withSenedoo = {{ with_senedoo }};

document.getElementById('start-info').innerHTML =
  `${total} questions disponibles.<br>` +
  `💡 ${withClaude} explications Claude · 📚 ${withSenedoo} explications Senedoo/Udemy<br>` +
  `Scoring : +1 bonne / −1 mauvaise / 0 saut.`;

const quizCertSelect = document.getElementById('quiz-cert-select');
const quizCertCount = document.getElementById('quiz-cert-count');
const quizModuleSelect = document.getElementById('quiz-module-select');
const quizIncludeHidden = document.getElementById('quiz-include-hidden');
let startBtnRef = null;
function getStartBtn() {
  if (!startBtnRef) startBtnRef = document.querySelector('#start-screen .btn-primary');
  return startBtnRef;
}

async function populateModules() {
  if (!quizModuleSelect) return;
  const cert = quizCertSelect ? quizCertSelect.value : '19.0';
  const includeHidden = quizIncludeHidden && quizIncludeHidden.checked ? '1' : '0';
  const url = `/api/modules?cert=${encodeURIComponent(cert)}&include_hidden=${includeHidden}`;
  let data;
  try {
    const res = await fetch(url);
    data = await res.json();
  } catch (e) { return; }
  const currentValue = quizModuleSelect.value;
  quizModuleSelect.innerHTML = '<option value="">— Choisis un module —</option>';
  // Group by tier for readability
  const tierLabel = { cert: 'Cert (obligatoire)', tier1: 'Tier 1 (fréquent)', tier2: 'Tier 2 (occasionnel)', other: 'Autres' };
  const byTier = { cert: [], tier1: [], tier2: [], other: [] };
  for (const m of (data.modules || [])) {
    if (m.count === 0) continue;
    (byTier[m.tier] || byTier.other).push(m);
  }
  for (const tier of ['cert', 'tier1', 'tier2', 'other']) {
    if (byTier[tier].length === 0) continue;
    const og = document.createElement('optgroup');
    og.label = tierLabel[tier] || tier;
    for (const m of byTier[tier]) {
      const opt = document.createElement('option');
      opt.value = m.module;
      const displayName = m.module === '_unclassified' ? 'Non classées (Udemy hors-scope)' : m.module;
      opt.textContent = `${displayName} (${m.count})`;
      opt.dataset.count = m.count;
      if (m.module === currentValue) opt.selected = true;
      og.appendChild(opt);
    }
    quizModuleSelect.appendChild(og);
  }
  updateQuizState();
}

function updateQuizState() {
  if (!quizModuleSelect) return;
  const mod = quizModuleSelect.value;
  const startBtn = getStartBtn();
  if (!mod) {
    if (startBtn) { startBtn.disabled = true; startBtn.style.opacity = '0.5'; }
    if (quizCertCount) quizCertCount.textContent = '— sélectionne un module —';
    document.getElementById('start-info').innerHTML =
      '<em>👆 Sélectionne un module en haut pour commencer.</em>';
    total = 0;
    return;
  }
  const opt = quizModuleSelect.options[quizModuleSelect.selectedIndex];
  const n = parseInt(opt.dataset.count || '0', 10);
  total = n;
  if (startBtn) { startBtn.disabled = n === 0; startBtn.style.opacity = n === 0 ? '0.5' : '1'; }
  const qc = document.getElementById('q-count');
  if (qc) { qc.max = Math.max(1, n); if (parseInt(qc.value, 10) > n) qc.value = Math.min(20, n); }
  if (quizCertCount) {
    const v = String(quizCertSelect.value || '19.0').replace('.0', '');
    const displayName = mod === '_unclassified' ? 'Non classées' : mod;
    quizCertCount.textContent = `${n} q. dans ${displayName} (cert v${v})`;
  }
  document.getElementById('start-info').innerHTML =
    `<strong>${n}</strong> questions disponibles dans <code>${mod === '_unclassified' ? 'Non classées' : mod}</code>.<br>` +
    `💡 ${withClaude} explications Claude · 📚 ${withSenedoo} explications Senedoo/Udemy<br>` +
    `Scoring : +1 bonne / −1 mauvaise / 0 saut.`;
}

if (quizCertSelect) {
  quizCertSelect.addEventListener('change', async function() {
    // Persiste la cert côté serveur, puis re-populate les modules
    try {
      await fetch('/api/settings/target_certification', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_certification: quizCertSelect.value })
      });
    } catch (e) { /* non-bloquant */ }
    await populateModules();
  });
}
if (quizModuleSelect) quizModuleSelect.addEventListener('change', updateQuizState);
if (quizIncludeHidden) quizIncludeHidden.addEventListener('change', populateModules);
// Init au chargement
populateModules();

async function startQuiz() {
  const mod = quizModuleSelect ? quizModuleSelect.value : '';
  if (!mod) { alert('Choisis un module avant de lancer le quiz.'); return; }
  const count = Math.min(parseInt(document.getElementById('q-count').value) || 20, total);
  const includeHidden = quizIncludeHidden && quizIncludeHidden.checked ? '1' : '0';
  const res = await fetch(`/api/questions?n=${count}&module=${encodeURIComponent(mod)}&include_hidden=${includeHidden}`);
  questions = await res.json();
  current = 0; score = 0; good = 0; bad = 0; skip = 0;
  document.getElementById('start-screen').style.display = 'none';
  document.getElementById('score-bar').style.display = 'flex';
  document.getElementById('question-card').style.display = 'block';
  startTime = Date.now();
  timerInterval = setInterval(updateTimer, 1000);
  showQuestion();
}

function updateTimer() {
  const s = Math.floor((Date.now() - startTime) / 1000);
  const m = Math.floor(s / 60);
  document.getElementById('timer').textContent =
    String(m).padStart(2,'0') + ':' + String(s % 60).padStart(2,'0');
}

function normalizeQuestionMediaRel(raw) {
  if (raw == null || typeof raw !== 'string') return '';
  let s = raw.trim().split('\\\\').join('/');
  if (!s || s.indexOf('..') >= 0 || s.indexOf(':') >= 0) return '';
  const low = s.toLowerCase();
  if (low.startsWith('/static/')) s = s.slice('/static/'.length);
  else if (low.startsWith('static/')) s = s.slice(7);
  while (s.startsWith('/')) s = s.slice(1);
  if (!s.startsWith('question_media/')) return '';
  return s;
}

function showQuestion() {
  if (current >= questions.length) { showResults(); return; }
  answered = false;
  const q = questions[current];
  document.getElementById('progress-text').textContent =
    `Question ${current+1} / ${questions.length}`;
  document.getElementById('question-text').textContent = q.title;
  const frEl = document.getElementById('question-text-fr');
  frEl.textContent = q.title_fr || '';
  frEl.style.display = (showFr && q.title_fr) ? 'block' : 'none';

  const mediaWrap = document.getElementById('question-media');
  const mediaImg = document.getElementById('question-image');
  const rel = normalizeQuestionMediaRel(q.question_image);
  if (rel) {
    mediaImg.onerror = function() {
      mediaImg.removeAttribute('src');
      mediaImg.onerror = null;
      mediaWrap.style.display = 'none';
    };
    mediaImg.alt = 'Illustration (tableau, capture…) pour répondre à la question';
    mediaWrap.style.display = 'block';
    mediaImg.src = '/static/' + rel + '?t=' + Date.now();
  } else {
    mediaImg.onerror = null;
    mediaImg.removeAttribute('src');
    mediaWrap.style.display = 'none';
  }

  document.getElementById('explanations').style.display = 'none';
  document.getElementById('expl-senedoo').style.display = 'none';
  document.getElementById('expl-claude').style.display = 'none';
  document.getElementById('btn-skip').style.display = 'inline-block';
  document.getElementById('btn-next').style.display = 'none';

  const flagBtn = document.getElementById('btn-flag');
  if (flagBtn) {
    flagBtn.disabled = false;
    flagBtn.textContent = '⚐ Signaler';
    flagBtn.classList.remove('done');
  }

  const div = document.getElementById('answers');
  div.innerHTML = '';
  (q.answers || []).forEach((a, i) => {
    const btn = document.createElement('button');
    btn.className = 'answer-btn';
    btn.innerHTML = `<span class="val-en">${a.value}</span>` +
      (a.value_fr ? `<span class="val-fr" style="display:${showFr ? 'block' : 'none'}">${a.value_fr}</span>` : '');
    btn.onclick = () => selectAnswer(i, a.is_correct);
    div.appendChild(btn);
  });
}

function toggleLang() {
  showFr = !showFr;
  const btn = document.getElementById('lang-btn');
  btn.classList.toggle('active', showFr);
  const q = questions[current];
  if (!q) return;
  const frEl = document.getElementById('question-text-fr');
  frEl.style.display = (showFr && q.title_fr) ? 'block' : 'none';
  document.querySelectorAll('.answer-btn .val-fr').forEach(el => {
    el.style.display = showFr ? 'block' : 'none';
  });
}

async function flagQuestion() {
  const q = questions[current];
  if (!q || q.id == null) return;
  if (!confirm('Signaler cette question comme erronée ?\\n\\nElle sera retirée du quiz et placée dans la file de relecture (page Admin).')) return;
  const reason = (prompt('Précisez si vous le souhaitez ce qui ne va pas (facultatif) :', '') || '').trim();
  const btn = document.getElementById('btn-flag');
  btn.disabled = true;
  btn.textContent = '⏳…';
  try {
    const res = await fetch('/api/bank/' + encodeURIComponent(q.id) + '/flag', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reason: reason})
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      btn.textContent = '✓ Signalée';
      btn.classList.add('done');
    } else {
      alert('Échec du signalement : ' + (data.error || res.status));
      btn.disabled = false;
      btn.textContent = '⚐ Signaler';
    }
  } catch (e) {
    alert('Erreur réseau : ' + e);
    btn.disabled = false;
    btn.textContent = '⚐ Signaler';
  }
}

function revealAnswer() {
  document.getElementById('btn-skip').style.display = 'none';
  document.getElementById('btn-next').style.display = 'inline-block';
  showExplanation();
}

function selectAnswer(idx, isCorrect) {
  if (answered) return;
  answered = true;
  const btns = document.querySelectorAll('.answer-btn');
  btns.forEach(b => b.disabled = true);

  const q = questions[current];
  q.answers.forEach((a, i) => {
    if (a.is_correct) btns[i].classList.add('correct');
  });

  if (isCorrect) { score++; good++; }
  else { btns[idx].classList.add('wrong'); score--; bad++; }

  updateScoreBar();
  revealAnswer();
}

function skipQuestion() {
  if (answered) return;
  answered = true;
  skip++;
  const q = questions[current];
  const btns = document.querySelectorAll('.answer-btn');
  q.answers.forEach((a, i) => {
    if (a.is_correct) btns[i].classList.add('missed');
    btns[i].disabled = true;
  });
  updateScoreBar();
  revealAnswer();
}

function nextQuestion() { current++; showQuestion(); }

function updateScoreBar() {
  document.getElementById('s-good').textContent = good;
  document.getElementById('s-bad').textContent = bad;
  document.getElementById('s-skip').textContent = skip;
  document.getElementById('s-total').textContent = score;
}

function showExplanation() {
  const q = questions[current];
  const hasSenedoo = !!q.explication_senedoo;
  const hasClaude  = !!q.explication_claude;
  if (!hasSenedoo && !hasClaude) return;

  document.getElementById('explanations').style.display = 'flex';

  if (hasSenedoo) {
    document.getElementById('expl-senedoo-text').textContent = q.explication_senedoo;
    document.getElementById('expl-senedoo').style.display = 'block';
  }
  if (hasClaude) {
    document.getElementById('expl-claude-text').textContent = q.explication_claude;
    document.getElementById('expl-claude').style.display = 'block';
  }
}

function openModal() {
  document.getElementById('modal-overlay').classList.add('open');
  document.getElementById('modal-answer').style.display = 'none';
  document.getElementById('modal-answer').textContent = '';
  document.getElementById('modal-question').value = '';
  document.getElementById('modal-question').focus();
}
function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
}
document.getElementById('modal-overlay').addEventListener('click', e => {
  if (e.target === document.getElementById('modal-overlay')) closeModal();
});
document.getElementById('modal-question').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) askClaude();
});
async function askClaude() {
  const q = document.getElementById('modal-question').value.trim();
  if (!q) return;
  const btn = document.getElementById('modal-submit');
  const answerEl = document.getElementById('modal-answer');
  btn.disabled = true;
  btn.textContent = '⏳…';
  answerEl.style.display = 'block';
  answerEl.textContent = 'Claude réfléchit…';
  const res = await fetch('/api/ask', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({question: q})
  });
  const data = await res.json();
  answerEl.textContent = data.answer || data.error || 'Erreur inconnue';
  btn.disabled = false;
  btn.textContent = 'Envoyer';
}

function showResults() {
  clearInterval(timerInterval);
  document.getElementById('question-card').style.display = 'none';
  document.getElementById('results').style.display = 'block';
  const pct = questions.length > 0 ? Math.round((good / questions.length) * 100) : 0;
  document.getElementById('final-score').textContent = `${score} pts`;
  document.getElementById('final-stats').innerHTML =
    `✔ ${good} bonnes &nbsp;|&nbsp; ✘ ${bad} mauvaises &nbsp;|&nbsp; — ${skip} passées<br>` +
    `Taux de réussite : ${pct}%`;
}

(function () {
  try {
    const p = new URLSearchParams(window.location.search);
    if (p.get('openAsk') === '1') {
      history.replaceState({}, '', '/');
      openModal();
    }
  } catch (e) { /* ignore */ }
})();
</script>
</body>
</html>"""

BANK_HTML = (Path(__file__).parent / "banque.html").read_text(encoding="utf-8")
CAPTURE_HTML = (Path(__file__).parent / "capture_udemy.html").read_text(encoding="utf-8")
CAPTURE_PREVIEW_HTML = (Path(__file__).parent / "capture_preview.html").read_text(encoding="utf-8")

# --- Page d'administration : revue des questions (unverified / flagged) ------

ADMIN_GATE_HTML = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin — accès restreint</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background:#f0f4ff; color:#1a1a2e; display:flex; align-items:center;
         justify-content:center; min-height:100vh; margin:0; }
  .box { background:#fff; padding:2rem; border-radius:12px; box-shadow:0 2px 12px rgba(0,0,0,.1);
         max-width:420px; width:90%; text-align:center; }
  h1 { color:#714B67; font-size:1.3rem; margin:0 0 1rem; }
  p { color:#64748b; font-size:.92rem; line-height:1.5; }
  input { width:100%; padding:.6rem; border:2px solid #e2e8f0; border-radius:8px;
          font-size:1rem; margin:1rem 0; }
  button { background:#714B67; color:#fff; border:none; border-radius:8px;
           padding:.6rem 1.4rem; font-weight:600; cursor:pointer; font-size:.95rem; }
  code { background:#f1f5f9; padding:.1rem .3rem; border-radius:4px; }
</style></head>
<body>
  <div class="box">
    <h1>🔧 Revue des questions</h1>
    {% if configured %}
      <p>Accès réservé. Entrez le jeton d'administration.</p>
      <form method="get" action="/admin/review">
        <input type="password" name="token" placeholder="Jeton admin" autofocus>
        <button type="submit">Entrer</button>
      </form>
    {% else %}
      <p>La page d'administration n'est pas configurée.<br>
      Ajoutez une section <code>"admin": {"token": "..."}</code> dans
      <code>config.json</code> puis redémarrez l'application.</p>
    {% endif %}
  </div>
</body></html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Revue des questions — v{{ app_version }}</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background:#f0f4ff; color:#1a1a2e; }
  header { background:#714B67; color:#fff; padding:1rem 1.5rem; display:flex;
           align-items:center; justify-content:space-between; flex-wrap:wrap; gap:.6rem; }
  header h1 { font-size:1.2rem; }
  header .v { font-size:.78rem; opacity:.9; margin-left:.5rem; }
  header nav a { color:#fff; text-decoration:none; background:rgba(255,255,255,.15);
                 border:1px solid rgba(255,255,255,.3); border-radius:8px;
                 padding:.4rem .8rem; font-size:.85rem; font-weight:600; margin-left:.4rem; }
  .toolbar { max-width:920px; margin:1.2rem auto 0; padding:0 1rem; display:flex;
             gap:.5rem; flex-wrap:wrap; align-items:center; }
  .tab { background:#fff; border:2px solid #e2e8f0; border-radius:999px; padding:.4rem 1rem;
         cursor:pointer; font-weight:600; font-size:.9rem; color:#475569; }
  .tab.active { background:#714B67; color:#fff; border-color:#714B67; }
  .tab .n { font-weight:700; }
  .refresh { margin-left:auto; background:#e2e8f0; border:none; border-radius:8px;
             padding:.45rem .9rem; cursor:pointer; font-weight:600; font-size:.85rem; }
  main { max-width:920px; margin:1rem auto 3rem; padding:0 1rem; }
  .empty { text-align:center; color:#64748b; padding:3rem 1rem; font-size:1.1rem; }
  .card { background:#fff; border-radius:12px; padding:1.25rem 1.4rem; margin-bottom:1.1rem;
          box-shadow:0 2px 8px rgba(0,0,0,.07); }
  .badges { display:flex; flex-wrap:wrap; gap:.4rem; margin-bottom:.7rem; }
  .badge { font-size:.72rem; font-weight:700; border-radius:999px; padding:.18rem .6rem; }
  .b-flag { background:#fee2e2; color:#b91c1c; }
  .b-unver { background:#fef3c7; color:#92400e; }
  .b-mod { background:#ede9fe; color:#5b21b6; }
  .b-ver { background:#e0f2fe; color:#075985; }
  .b-score { background:#f1f5f9; color:#475569; }
  .b-id { background:#f8fafc; color:#94a3b8; }
  .flag-reason { background:#fff1f2; border-left:3px solid #fb7185; color:#9f1239;
                 padding:.4rem .7rem; border-radius:0 6px 6px 0; font-size:.85rem; margin-bottom:.7rem; }
  .q-title { font-size:1.05rem; font-weight:600; line-height:1.45; }
  .q-title-fr { font-size:.92rem; color:#64748b; font-style:italic; margin-top:.2rem; }
  .answers { list-style:none; margin:.9rem 0 0; }
  .ans { display:flex; gap:.5rem; align-items:flex-start; padding:.4rem .6rem; border-radius:8px;
         border:1px solid #eef2f7; margin-bottom:.35rem; }
  .ans.correct { background:#f0fdf4; border-color:#bbf7d0; }
  .ans .mark { color:#94a3b8; font-weight:700; }
  .ans.correct .mark { color:#16a34a; }
  .atext { display:flex; flex-direction:column; }
  .aen { font-size:.92rem; }
  .afr { font-size:.82rem; color:#64748b; font-style:italic; }
  .expl { margin-top:.8rem; font-size:.88rem; }
  .expl summary { cursor:pointer; color:#714B67; font-weight:600; }
  .expl-s { background:#fff8e1; border-left:3px solid #f59e0b; padding:.5rem .7rem;
            border-radius:0 6px 6px 0; margin-top:.5rem; white-space:pre-wrap; }
  .expl-c { background:#f0f4ff; border-left:3px solid #714B67; padding:.5rem .7rem;
            border-radius:0 6px 6px 0; margin-top:.5rem; white-space:pre-wrap; }
  .actions { display:flex; gap:.6rem; flex-wrap:wrap; margin-top:1rem; }
  .btn { border:none; border-radius:8px; padding:.5rem 1rem; cursor:pointer;
         font-weight:600; font-size:.88rem; }
  .b-validate { background:#16a34a; color:#fff; }
  .b-edit { background:#e2e8f0; color:#1a1a2e; }
  .b-del { background:#fee2e2; color:#b91c1c; }
  .b-cancel { background:#e2e8f0; color:#1a1a2e; }
  .edit-wrap { margin-top:1rem; }
  .editform { background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px; padding:1rem; }
  .editform label { display:block; font-size:.78rem; font-weight:700; text-transform:uppercase;
                    letter-spacing:.03em; color:#64748b; margin:.7rem 0 .25rem; }
  .editform textarea, .editform input[type=text] {
    width:100%; padding:.5rem; border:2px solid #e2e8f0; border-radius:8px;
    font-family:inherit; font-size:.92rem; }
  .editform textarea { min-height:54px; resize:vertical; }
  .arow { display:flex; gap:.5rem; align-items:center; margin-bottom:.4rem; }
  .arow input[type=text] { flex:1; }
  .arow input[type=radio] { width:18px; height:18px; }
  .opt { display:flex !important; align-items:center; gap:.4rem; text-transform:none !important;
         letter-spacing:0 !important; font-size:.85rem !important; color:#1a1a2e !important; margin-top:.8rem !important; }
  .editbar { display:flex; gap:.6rem; margin-top:1rem; }
  .toast { position:fixed; bottom:1.5rem; left:50%; transform:translateX(-50%);
           background:#1a1a2e; color:#fff; padding:.7rem 1.2rem; border-radius:8px;
           font-size:.9rem; opacity:0; transition:opacity .25s; pointer-events:none; }
  .toast.show { opacity:1; }
</style></head>
<body>
<header>
  <div><h1 style="display:inline">🔧 Revue des questions</h1><span class="v">v{{ app_version }}</span></div>
  <nav><a href="/">🎓 Quiz</a><a href="/banque">📋 Banque</a></nav>
</header>
<div class="toolbar">
  <button class="tab active" data-s="all" onclick="load('all')">Tout</button>
  <button class="tab" data-s="unverified" onclick="load('unverified')">À revoir <span class="n" id="c-unverified">0</span></button>
  <button class="tab" data-s="flagged" onclick="load('flagged')">Signalées <span class="n" id="c-flagged">0</span></button>
  <button class="refresh" onclick="load(CURRENT)">↻ Rafraîchir</button>
</div>
<main><div id="list"></div></main>
<div class="toast" id="toast"></div>
<script>
let CACHE = {}, CURRENT = 'all';

function el(tag, props, kids) {
  const n = document.createElement(tag);
  if (props) for (const k in props) {
    if (k === 'class') n.className = props[k];
    else if (k === 'text') n.textContent = props[k];
    else n.setAttribute(k, props[k]);
  }
  (kids || []).forEach(c => { if (c != null) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
  return n;
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1800);
}

async function load(status) {
  CURRENT = status || 'all';
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.s === CURRENT));
  const list = document.getElementById('list');
  list.textContent = 'Chargement…';
  let data;
  try {
    const res = await fetch('/api/admin/review?status=' + encodeURIComponent(CURRENT));
    if (res.status === 403) { list.textContent = 'Session expirée — rechargez la page avec le jeton (?token=…).'; return; }
    data = await res.json();
  } catch (e) { list.textContent = 'Erreur réseau : ' + e; return; }
  document.getElementById('c-unverified').textContent = data.counts.unverified;
  document.getElementById('c-flagged').textContent = data.counts.flagged;
  CACHE = {};
  (data.questions || []).forEach(q => { CACHE[q.id] = q; });
  render(data.questions || []);
}

function render(items) {
  const list = document.getElementById('list');
  list.textContent = '';
  if (!items.length) { list.appendChild(el('p', {class:'empty', text:'Rien à revoir ici 🎉'})); return; }
  items.forEach(q => list.appendChild(card(q)));
}

function card(q) {
  const c = el('div', {class:'card', id:'card-' + q.id});
  const badges = el('div', {class:'badges'});
  badges.appendChild(el('span', {class:'badge ' + (q.status === 'flagged' ? 'b-flag' : 'b-unver'),
                                 text: q.status === 'flagged' ? '⚐ Signalée' : 'À revoir (3/5)'}));
  if (q.module) badges.appendChild(el('span', {class:'badge b-mod', text:q.module}));
  if (q.target_version) badges.appendChild(el('span', {class:'badge b-ver', text:'v' + q.target_version}));
  if (q.judge_score != null) badges.appendChild(el('span', {class:'badge b-score', text:'judge ' + q.judge_score + '/5'}));
  badges.appendChild(el('span', {class:'badge b-id', text:'#' + q.id}));
  c.appendChild(badges);
  if (q.flag_reason) c.appendChild(el('div', {class:'flag-reason', text:'⚐ Motif : ' + q.flag_reason}));
  c.appendChild(el('div', {class:'q-title', text:q.title || ''}));
  if (q.title_fr) c.appendChild(el('div', {class:'q-title-fr', text:q.title_fr}));
  const ul = el('ul', {class:'answers'});
  (q.answers || []).forEach(a => {
    const li = el('li', {class: a.is_correct ? 'ans correct' : 'ans'});
    li.appendChild(el('span', {class:'mark', text: a.is_correct ? '✓' : '○'}));
    const tw = el('span', {class:'atext'});
    tw.appendChild(el('span', {class:'aen', text:a.value || ''}));
    if (a.value_fr) tw.appendChild(el('span', {class:'afr', text:a.value_fr}));
    li.appendChild(tw);
    ul.appendChild(li);
  });
  c.appendChild(ul);
  if (q.explication_senedoo || q.explication_claude) {
    const det = el('details', {class:'expl'});
    det.appendChild(el('summary', {text:'Explications'}));
    if (q.explication_senedoo) det.appendChild(el('div', {class:'expl-s', text:'📚 ' + q.explication_senedoo}));
    if (q.explication_claude) det.appendChild(el('div', {class:'expl-c', text:'🤖 ' + q.explication_claude}));
    c.appendChild(det);
  }
  const act = el('div', {class:'actions'});
  const bV = el('button', {class:'btn b-validate', text:'✅ Valider'}); bV.onclick = () => validate(q.id);
  const bE = el('button', {class:'btn b-edit', text:'✏️ Modifier'}); bE.onclick = () => toggleEdit(q.id);
  const bD = el('button', {class:'btn b-del', text:'🗑️ Supprimer'}); bD.onclick = () => del(q.id);
  act.appendChild(bV); act.appendChild(bE); act.appendChild(bD);
  c.appendChild(act);
  c.appendChild(el('div', {class:'edit-wrap', id:'edit-' + q.id, style:'display:none'}));
  return c;
}

async function validate(id) {
  const res = await fetch('/api/admin/questions/' + id + '/validate', {method:'POST'});
  const d = await res.json().catch(() => ({}));
  if (res.ok && d.ok) { toast('✅ Question #' + id + ' validée'); load(CURRENT); }
  else alert('Échec : ' + (d.error || res.status));
}

async function del(id) {
  if (!confirm('Supprimer définitivement la question #' + id + ' ?\\n\\nCette action est irréversible.')) return;
  const res = await fetch('/api/admin/questions/' + id, {method:'DELETE'});
  const d = await res.json().catch(() => ({}));
  if (res.ok && d.ok) { toast('🗑️ Question #' + id + ' supprimée'); load(CURRENT); }
  else alert('Échec : ' + (d.error || res.status));
}

function toggleEdit(id) {
  const wrap = document.getElementById('edit-' + id);
  if (wrap.style.display === 'block') { wrap.style.display = 'none'; wrap.textContent = ''; return; }
  const q = CACHE[id];
  wrap.textContent = '';
  const form = el('div', {class:'editform'});
  form.appendChild(el('label', {text:'Énoncé (EN)'}));
  const tEn = el('textarea', {class:'f-title'}); tEn.value = q.title || ''; form.appendChild(tEn);
  form.appendChild(el('label', {text:'Énoncé (FR)'}));
  const tFr = el('textarea', {class:'f-title-fr'}); tFr.value = q.title_fr || ''; form.appendChild(tFr);
  form.appendChild(el('label', {text:'Réponses (cocher la bonne)'}));
  const rname = 'correct-' + id, arows = [];
  (q.answers || []).forEach(a => {
    const row = el('div', {class:'arow'});
    const r = el('input', {type:'radio', name:rname}); r.checked = !!a.is_correct;
    const ie = el('input', {type:'text', placeholder:'EN'}); ie.value = a.value || '';
    const ifr = el('input', {type:'text', placeholder:'FR'}); ifr.value = a.value_fr || '';
    row.appendChild(r); row.appendChild(ie); row.appendChild(ifr);
    form.appendChild(row);
    arows.push({a, r, ie, ifr});
  });
  form.appendChild(el('label', {text:'Explication Senedoo / Udemy'}));
  const eS = el('textarea'); eS.value = q.explication_senedoo || ''; form.appendChild(eS);
  form.appendChild(el('label', {text:'Explication Claude'}));
  const eC = el('textarea'); eC.value = q.explication_claude || ''; form.appendChild(eC);
  const opt = el('label', {class:'opt'});
  const chk = el('input', {type:'checkbox'}); chk.checked = true;
  opt.appendChild(chk); opt.appendChild(document.createTextNode(' Valider après enregistrement'));
  form.appendChild(opt);
  const bar = el('div', {class:'editbar'});
  const save = el('button', {class:'btn b-validate', text:'💾 Enregistrer'});
  save.onclick = () => saveEdit(id, {tEn, tFr, arows, eS, eC, chk});
  const cancel = el('button', {class:'btn b-cancel', text:'Annuler'});
  cancel.onclick = () => { wrap.style.display = 'none'; wrap.textContent = ''; };
  bar.appendChild(save); bar.appendChild(cancel);
  form.appendChild(bar);
  wrap.appendChild(form);
  wrap.style.display = 'block';
}

async function saveEdit(id, f) {
  const title = f.tEn.value.trim();
  if (!title) { alert('L\\'énoncé EN est obligatoire.'); return; }
  const answers = f.arows.map(x => ({
    id: x.a.id, value: x.ie.value.trim(), value_fr: x.ifr.value.trim(), is_correct: x.r.checked,
  }));
  if (answers.some(a => !a.value)) { alert('Chaque réponse doit avoir un texte EN.'); return; }
  if (answers.filter(a => a.is_correct).length !== 1) { alert('Cochez exactement une bonne réponse.'); return; }
  const payload = {
    title, title_fr: f.tFr.value.trim(), answers,
    explication_senedoo: f.eS.value, explication_claude: f.eC.value,
  };
  const res = await fetch('/api/bank/' + id, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const d = await res.json().catch(() => ({}));
  if (!(res.ok && d.ok)) { alert('Échec enregistrement : ' + (d.error || res.status)); return; }
  if (f.chk.checked) await fetch('/api/admin/questions/' + id + '/validate', {method:'POST'});
  toast('💾 Question #' + id + ' enregistrée');
  load(CURRENT);
}

load('all');
</script>
</body></html>"""

ALLOWED_CAPTURE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
PENDING_CAPTURE_ROOT = Path(tempfile.gettempdir()) / "odoo_quiz_capture_pending"
FULLPAGE_INBOX = PENDING_CAPTURE_ROOT / "fullpage_inbox"
FULLPAGE_MAX_BYTES = 12 * 1024 * 1024


def _valid_pending_token(tid: str) -> bool:
    t = (tid or "").strip().lower()
    return len(t) == 32 and all(c in "0123456789abcdef" for c in t)


def _allowed_fullpage_cors_origin(origin: str) -> bool:
    """Origines autorisées pour l’upload depuis le favori (Odoo / Udemy → localhost)."""
    o = (origin or "").strip()
    if not o:
        return False
    try:
        from urllib.parse import urlparse

        p = urlparse(o)
        host = (p.hostname or "").lower()
        if p.scheme not in ("http", "https"):
            return False
        if host in ("127.0.0.1", "localhost"):
            return True
        if host == "odoo.com" or host.endswith(".odoo.com"):
            return p.scheme == "https"
        if host == "udemy.com" or host.endswith(".udemy.com"):
            return p.scheme == "https"
    except (ValueError, TypeError):
        return False
    return False


def _fullpage_cors(resp: Response) -> Response:
    origin = request.headers.get("Origin")
    if origin and _allowed_fullpage_cors_origin(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Vary"] = "Origin"
    return resp


def _fullpage_inbox_path(tid: str) -> Path:
    return FULLPAGE_INBOX / f"{tid}.bin"


def _prune_stale_fullpage_inbox(max_age_s: float = 3600) -> None:
    import time

    if not FULLPAGE_INBOX.is_dir():
        return
    now = time.time()
    for p in FULLPAGE_INBOX.glob("*.bin"):
        try:
            if now - p.stat().st_mtime > max_age_s:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def _pending_path(tid: str) -> Path:
    return PENDING_CAPTURE_ROOT / tid


def _prune_stale_pending(max_age_s: float = 43200) -> None:
    import time

    if not PENDING_CAPTURE_ROOT.is_dir():
        return
    now = time.time()
    for sub in PENDING_CAPTURE_ROOT.iterdir():
        if not sub.is_dir():
            continue
        try:
            if now - sub.stat().st_mtime > max_age_s:
                shutil.rmtree(sub, ignore_errors=True)
        except OSError:
            pass


def _parse_capture_confirm_form(form, only_index: Optional[int] = None) -> list[dict]:
    """Reconstruit la liste d’items depuis le formulaire de validation capture."""
    raw_n = (form.get("capture_n") or "0").strip()
    if not raw_n.isdigit():
        raise ValueError("Formulaire invalide (nombre d’extractions).")
    n = int(raw_n)
    if n < 1 or n > 40:
        raise ValueError("Nombre d’extractions non pris en charge.")
    if only_index is not None and (only_index < 0 or only_index >= n):
        raise ValueError("Fiche à enregistrer invalide.")
    out: list[dict] = []
    for i in range(n):
        if only_index is not None and i != only_index:
            continue
        p = f"c{i}_"
        title = (form.get(f"{p}title") or "").strip()
        n_ans_s = (form.get(f"{p}n_answers") or "0").strip()
        if not n_ans_s.isdigit():
            raise ValueError(f"Carte {i + 1} : nombre de réponses invalide.")
        n_ans = int(n_ans_s)
        if n_ans < 2 or n_ans > 20:
            raise ValueError(f"Carte {i + 1} : entre 2 et 20 réponses requises.")
        answers: list[str] = []
        answers_fr: list[str] = []
        for j in range(n_ans):
            answers.append((form.get(f"{p}a{j}_en") or "").strip())
            answers_fr.append((form.get(f"{p}a{j}_fr") or "").strip())
        # On détermine D'ABORD le choix (ajouter / mettre à jour / ignorer) :
        # une carte « ignorée » ne doit PAS exiger qu'une bonne réponse soit
        # sélectionnée (sinon le bouton « Ignorer » échoue avec
        # « choisissez la bonne réponse… »).
        is_dup = form.get(f"{p}is_dup") == "1"
        skip_bank_update = False
        skip_new_question = False
        force_new_despite_bank_dup = False
        if is_dup:
            ch = (form.get(f"{p}dup_choice") or "").strip()
            if ch not in ("update", "ignore", "add_new"):
                raise ValueError(
                    f"Carte {i + 1} : pour une question déjà en banque, choisissez « Mettre à jour », « Ignorer » ou « Ajouter comme nouvelle question »."
                )
            skip_bank_update = ch == "ignore"
            force_new_despite_bank_dup = ch == "add_new"
        else:
            ch = (form.get(f"{p}new_choice") or "").strip()
            if ch not in ("add", "ignore"):
                raise ValueError(
                    f"Carte {i + 1} : pour une nouvelle question, choisissez « Ajouter à la banque » ou « Ignorer »."
                )
            skip_new_question = ch == "ignore"
        card_ignored = skip_new_question or (is_dup and skip_bank_update)

        raw_correct = (form.get(f"{p}correct") or "").strip()
        if raw_correct.lower() in ("none", "null"):
            raw_correct = ""
        if not raw_correct.isdigit():
            if card_ignored:
                correct_index = None  # non utilisé : la carte est ignorée
            else:
                raise ValueError(
                    f"Carte {i + 1} : choisissez la bonne réponse (une option entre 1 et {n_ans})."
                )
        else:
            correct_index = int(raw_correct)
            if correct_index < 1 or correct_index > n_ans:
                raise ValueError(f"Carte {i + 1} : numéro de bonne réponse hors plage.")
        ex_raw_id = (form.get(f"{p}existing_id") or "").strip()
        existing_id = int(ex_raw_id) if ex_raw_id.isdigit() else None
        crop_raw = (form.get(f"{p}crop_json") or "{}").strip()
        try:
            crop_rel = json.loads(html.unescape(crop_raw))
        except json.JSONDecodeError:
            crop_rel = None
        if crop_rel is not None and not isinstance(crop_rel, dict):
            crop_rel = None
        needs_vals = form.getlist(f"{p}needs_image")
        needs_question_image = any(str(v).strip() == "1" for v in needs_vals)
        raw_sug = (form.get(f"{p}suggested_ci") or "").strip()
        suggested_correct_index = int(raw_sug) if raw_sug.isdigit() else None
        suggested_correct_source = (form.get(f"{p}suggested_src") or "").strip()
        if suggested_correct_source not in ("udemy", "odoo", "claude", "user", ""):
            suggested_correct_source = ""
        out.append(
            {
                "title": title,
                "answers": answers,
                "correct_index": correct_index,
                "explication_udemy": (form.get(f"{p}exp_udemy") or "").strip(),
                "title_fr": (form.get(f"{p}title_fr") or "").strip(),
                "answers_fr": answers_fr,
                "explication_claude": (form.get(f"{p}exp_claude") or "").strip(),
                "existing_id": existing_id,
                "crop_rel": crop_rel,
                "needs_question_image": needs_question_image,
                "skip_bank_update": skip_bank_update,
                "skip_new_question": skip_new_question,
                "force_new_despite_bank_dup": force_new_despite_bank_dup,
                "suggested_correct_index": suggested_correct_index,
                "suggested_correct_source": suggested_correct_source or None,
            }
        )
    return out


def _normalize_capture_source(raw: str) -> str:
    s = (raw or "udemy").strip().lower()
    return "odoo" if s in ("odoo", "odoo_web", "website", "elearning", "slides") else "udemy"


def _load_capture_source_preference() -> str:
    cap = CFG.get("capture")
    if isinstance(cap, dict) and cap.get("default_source"):
        return _normalize_capture_source(str(cap.get("default_source")))
    return "udemy"


def _save_capture_source_preference(source: str) -> None:
    """Mémorise Udemy / Odoo dans config.json pour les prochaines visites."""
    global CFG
    src = _normalize_capture_source(source)
    cap = CFG.get("capture")
    if isinstance(cap, dict) and cap.get("default_source") == src:
        return
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = dict(CFG) if isinstance(CFG, dict) else {}
    block = data.get("capture")
    if not isinstance(block, dict):
        block = {}
    if block.get("default_source") == src:
        CFG = data
        return
    block["default_source"] = src
    data["capture"] = block
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    CFG = data


def _save_pending_capture(
    items: list[dict],
    screenshot_src: str,
    ext: str,
    vision_notice: str = "",
    capture_source: str = "udemy",
) -> str:
    _prune_stale_pending()
    PENDING_CAPTURE_ROOT.mkdir(parents=True, exist_ok=True)
    tid = uuid.uuid4().hex
    d = _pending_path(tid)
    d.mkdir(exist_ok=False)
    with open(d / "items.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    shutil.copy2(screenshot_src, d / f"screen.{ext}")
    with open(d / "meta.json", "w", encoding="utf-8") as f:
        json.dump(
            {"vision_notice": vision_notice or "", "capture_source": _normalize_capture_source(capture_source)},
            f,
            ensure_ascii=False,
            indent=2,
        )
    return tid


def _load_pending_capture(tid: str):
    if not _valid_pending_token(tid):
        return None
    d = _pending_path(tid)
    if not d.is_dir():
        return None
    items_path = d / "items.json"
    if not items_path.is_file():
        return None
    hits = sorted(d.glob("screen.*"))
    if not hits:
        return None
    with open(items_path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        return None
    meta: dict[str, Any] = {}
    meta_path = d / "meta.json"
    if meta_path.is_file():
        try:
            with open(meta_path, encoding="utf-8") as mf:
                loaded = json.load(mf)
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, json.JSONDecodeError):
            meta = {}
    return raw, str(hits[0]), meta


def _clear_pending(tid: str) -> None:
    if not _valid_pending_token(tid):
        return
    shutil.rmtree(_pending_path(tid), ignore_errors=True)


def _save_pending_items_list(tid: str, items: list[dict]) -> None:
    if not _valid_pending_token(tid):
        raise ValueError("Session capture invalide.")
    d = _pending_path(tid)
    if not d.is_dir():
        raise ValueError("Session capture expirée.")
    with open(d / "items.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _enrich_step_outcome_fr(item: dict) -> tuple[str, str]:
    """Libellé + état UI (done | warn | error) après enrich_item_for_preview."""
    prov = (item.get("correct_suggestion_source") or "").strip()
    if prov == "aucune":
        return "Pas de suggestion par Claude — choix de la bonne réponse sur l’écran de validation.", "warn"
    ms = (item.get("match_source") or "").strip()
    if ms in ("banque",) or item.get("bank_registered_answer"):
        return "Complément : données issues de la banque (pas d’appel texte Anthropic).", "done"
    if item.get("bank_answer_agrees_claude") is True and item.get("in_banque"):
        return (
            "Claude a proposé la réponse (alignée avec la fiche banque identique détectée).",
            "done",
        )
    if item.get("in_banque") and item.get("bank_answer_agrees_claude") is False:
        return (
            "Claude a proposé une réponse différente de la fiche banque (doublon strict) — vérifiez avant fusion.",
            "warn",
        )
    if ms == "banque_sans_bonne_reponse":
        return "Complément : alignement banque ; Anthropic seulement si la bonne réponse manque.", "done"
    if ms == "sans_api":
        return "Complément : API Anthropic absente — saisie utilisateur.", "warn"
    if ms == "claude_api":
        return "Complément Anthropic : réponse reçue.", "done"
    if ms == "claude_api_timeout":
        return "Complément Anthropic : délai dépassé (timeout).", "error"
    if ms == "claude_api_surcharge":
        return "Complément Anthropic : serveurs saturés.", "error"
    if ms == "claude_api_erreur":
        return "Complément Anthropic : erreur ou réponse inutilisable.", "error"
    if ms == "claude_incertain":
        return (
            "Claude peu confiant : aucune suggestion automatique — choisissez la bonne option à la main.",
            "warn",
        )
    return "Complément : terminé.", "done"


def _capture_progress_line(
    step_id: str,
    state: str,
    title: str,
    detail: str = "",
    **extra: Any,
) -> bytes:
    """Une ligne NDJSON pour l’overlay de progression (title + detail + label pour compatibilité)."""
    row: dict[str, Any] = {
        "type": "step",
        "id": step_id,
        "state": state,
        "title": title,
        "detail": detail,
        "label": (f"{title} — {detail}" if detail else title).strip(),
    }
    row.update(extra)
    return (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")


def _build_preview_blocks_from_saved_items(items: list[dict], tmp_path: str) -> list[dict]:
    bank_rows = ge.load().get("questions", [])
    if not isinstance(bank_rows, list):
        bank_rows = []
    preview_blocks: list[dict] = []
    for it in items:
        img = preview_region_data_url(
            tmp_path,
            it.get("crop_rel"),
            bool(it.get("needs_question_image")),
            max_edge=2000,
            max_edge_full=2400,
            quality=82,
        )
        ci = it.get("correct_index")
        afr = it.get("answers_fr")
        if not isinstance(afr, list):
            afr = []
        merge_ctx = bank_capture_merge_context(it, bank_rows)
        need_img = bool(it.get("needs_question_image"))
        cr = it.get("crop_rel")
        has_tight_crop = False
        if need_img and isinstance(cr, dict):
            try:
                w = float(cr.get("width", 0))
                h = float(cr.get("height", 0))
                has_tight_crop = w >= 0.02 and h >= 0.02
            except (TypeError, ValueError):
                pass
        zoom_img = img or ""
        if need_img and has_tight_crop and img:
            zfull = preview_region_data_url(
                tmp_path,
                None,
                False,
                max_edge_full=2400,
                quality=82,
            )
            if zfull:
                zoom_img = zfull
        preview_blocks.append(
            {
                "title": it["title"],
                "answers": it["answers"],
                "correct_index": ci,
                "explication_udemy": (it.get("explication_udemy") or "").strip(),
                "title_fr": (it.get("title_fr") or "").strip(),
                "answers_fr": afr,
                "explication_claude": (it.get("explication_claude") or "").strip(),
                "match_source": (it.get("match_source") or "").strip(),
                "correct_suggestion_source": (it.get("correct_suggestion_source") or "").strip(),
                "bank_answer_provenance": (it.get("bank_answer_provenance") or "").strip(),
                "bank_answer_line_fr": (it.get("bank_answer_line_fr") or "").strip(),
                "bank_registered_answer": False,
                "bank_dup_title_only": bool(it.get("bank_dup_title_only")),
                "bank_duplicate_score": it.get("bank_duplicate_score"),
                "bank_duplicate_reason": (it.get("bank_duplicate_reason") or "").strip(),
                "bank_prior_correct_index": it.get("bank_prior_correct_index"),
                "bank_answer_agrees_claude": it.get("bank_answer_agrees_claude"),
                "suggested_correct_index": it.get("suggested_correct_index"),
                "suggested_correct_source": (it.get("suggested_correct_source") or "").strip(),
                "needs_image": need_img,
                "has_balance_crop": has_tight_crop,
                "balance_context_hint": title_suggests_balance_context(it.get("title", "")) and not need_img,
                "screen_context_hint": title_requires_capture_image(it.get("title", "")) and not need_img,
                "image_data_url": img or "",
                "zoom_image_data_url": zoom_img or (img or ""),
                "in_banque": bool(it.get("in_banque")),
                "existing_id": it.get("existing_id"),
                "crop_rel": it.get("crop_rel"),
                "rag_similar": it.get("rag_similar") if isinstance(it.get("rag_similar"), list) else [],
                "rag_search_mode": (it.get("rag_search_mode") or "texte").strip(),
                "rag_prompt_min_score": it.get("rag_prompt_min_score"),
                **merge_ctx,
            }
        )
    return preview_blocks


def _iter_capture_pipeline_ndjson_from_path(
    tmp_path: str,
    ext: str,
    capture_source: str = "udemy",
    *,
    dom_payload: Any = None,
    page_host: str = "",
) -> Iterator[bytes]:
    """Événements NDJSON ; l’image doit déjà être sur disque (évite fichier upload fermé en streaming)."""
    cap_src = _normalize_capture_source(capture_source)
    try:
        yield _capture_progress_line(
            "vision",
            "running",
            "1 · Extraction",
            "Lecture de la capture (DOM si disponible, sinon Claude Vision).",
        )

        vision_notice = ""
        try:
            items, cap_src, extract_method = extract_items_from_capture(
                tmp_path,
                dom_payload=dom_payload,
                page_host=page_host,
                capture_source=capture_source,
            )
            n_vision = len(items)
            if extract_method == "dom":
                vision_done_detail = (
                    f"{n_vision} question(s) lue(s) depuis la page (DOM)."
                    if n_vision > 1
                    else "Texte lu depuis la page (DOM) ; suite : banque puis complément."
                )
                done_title = "1 · Extraction DOM terminée"
            else:
                if n_vision > 1:
                    vision_done_detail = (
                        f"{n_vision} questions distinctes extraites de la même capture "
                        f"(ordre haut → bas). Une fiche de validation par question."
                    )
                else:
                    vision_done_detail = "Texte extrait ; suite : banque puis complément."
                done_title = "1 · Vision terminée"
            yield _capture_progress_line(
                "vision",
                "done",
                done_title,
                vision_done_detail,
            )
        except CaptureNoQuizContentError as e:
            no_quiz_msg = e.user_message()
            yield _capture_progress_line(
                "vision",
                "error",
                "1 · Aucune question sur la capture",
                no_quiz_msg,
                code="no_quiz_content",
            )
            yield (
                json.dumps(
                    {
                        "type": "no_quiz",
                        "message": no_quiz_msg,
                        "reason": e.reason,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            return
        except (RuntimeError, OSError, ValueError) as e:
            msg = str(e)
            low = msg.lower()
            if "dépasse le délai" in msg or "délai de" in low:
                vision_err = "Timeout Anthropic (vision)."
                vcode = "vision_timeout"
            elif "529" in msg or "satur" in low or "overloaded" in low:
                vision_err = "Serveurs Anthropic saturés (vision)."
                vcode = "vision_overload"
            else:
                vision_err = "Erreur Anthropic ou réponse inutilisable (vision)."
                vcode = "vision_error"
            yield _capture_progress_line(
                "vision",
                "error",
                "1 · Vision — échec",
                vision_err,
                code=vcode,
            )
            vision_notice = (
                "L’analyse automatique de la capture (API Anthropic) n’a pas abouti. "
                "La fiche ci‑dessous est préremplie : remplacez la question et les réponses par le texte réel, "
                "puis sélectionnez la bonne option."
            )
            items = [{**manual_vision_fallback_udemy_item(), "_capture_source": cap_src}]
            yield _capture_progress_line(
                "vision",
                "warn",
                "1 · Saisie manuelle",
                "Remplacez le texte factice par la question réelle et la bonne option.",
                code="vision_manual",
            )

        yield _capture_progress_line(
            "bank",
            "running",
            "Étape 2 — Chargement de la banque",
            "Lecture locale du fichier des questions (questions.json). "
            "La recherche de doublon (étape 3) utilise uniquement le titre extrait à l’étape 1.",
        )

        bank_rows = ge.load().get("questions", [])
        if not isinstance(bank_rows, list):
            bank_rows = []
        yield _capture_progress_line(
            "bank",
            "done",
            "Étape 2 — Banque prête",
            f"{len(bank_rows)} question(s) chargées. Étape suivante : comparaison du titre extrait avec ces titres.",
        )

        n_items = len(items)
        for i in range(n_items):
            yield _capture_progress_line(
                "dup",
                "running",
                f"Étape 3 — Doublon banque (fiche {i + 1} / {n_items})",
                "Comparaison du titre normalisé (clé de doublon) avec les titres déjà en banque.",
                card=i,
            )
            dup, qid, dup_sc, dup_reason = bank_identical_meta(
                items[i]["title"],
                items[i].get("answers") or [],
                bank_rows,
            )
            items[i] = {
                **items[i],
                "in_banque": dup,
                "existing_id": qid,
                "bank_duplicate_score": dup_sc,
                "bank_duplicate_reason": dup_reason,
            }
            if dup:
                pct = int(round(dup_sc * 100))
                dup_detail = (
                    f"Question identique en banque (id {qid}, score {pct} %, {dup_reason}). "
                    "Claude tranchera quand même la réponse à l’étape suivante."
                )
            else:
                dup_detail = (
                    "Pas de doublon strict (titre + options ou score ≥ seuil) : "
                    "nouvelle question ou variante — Claude proposera la réponse."
                )
            yield _capture_progress_line(
                "dup",
                "done",
                f"Étape 3 — Résultat fiche {i + 1} / {n_items}",
                dup_detail,
                card=i,
            )

        for i in range(n_items):
            yield _capture_progress_line(
                "enrich",
                "running",
                f"Étape 4 — Complément texte Claude / Anthropic (fiche {i + 1} / {n_items})",
                "Modèle texte (API) : traductions FR, explication et bonne réponse via Claude "
                "(appel systématique si l’API est disponible, avec contexte RAG banque).",
                card=i,
            )
            items[i] = enrich_item_for_preview(items[i], bank_rows, screenshot_path=tmp_path)
            lbl, st = _enrich_step_outcome_fr(items[i])
            yield _capture_progress_line(
                "enrich",
                st,
                f"Étape 4 — Résultat fiche {i + 1} / {n_items}",
                lbl,
                card=i,
            )

        multi_notice = ""
        if n_items > 1:
            multi_notice = (
                f"{n_items} questions détectées sur cette capture — "
                f"validez ou ignorez chaque fiche ci-dessous (de haut en bas)."
            )
        tid = _save_pending_capture(
            items,
            tmp_path,
            ext,
            vision_notice=(vision_notice or multi_notice),
            capture_source=cap_src,
        )
        yield (
            json.dumps(
                {"type": "done", "pending_id": tid, "n_cards": n_items},
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    except Exception as e:
        yield (
            json.dumps({"type": "fatal", "message": str(e)}, ensure_ascii=False) + "\n"
        ).encode("utf-8")


def _render_capture(error="", notice="", success="", default_source="udemy"):
    return render_template_string(
        CAPTURE_HTML,
        app_version=APP_VERSION,
        error=error,
        notice=notice,
        success=success,
        default_capture_source=_normalize_capture_source(default_source),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    from app.config import count_questions_for_cert, get_target_certification

    with_claude = sum(1 for q in ALL_QUESTIONS if q.get("explication_claude"))
    with_senedoo = sum(1 for q in ALL_QUESTIONS if q.get("explication_senedoo"))
    cert = get_target_certification()
    cert_counts = count_questions_for_cert(ALL_QUESTIONS, cert)
    return render_template_string(
        HTML,
        total=cert_counts["matched"],
        total_bank=cert_counts["total_bank"],
        target_certification=cert,
        with_claude=with_claude,
        with_senedoo=with_senedoo,
        app_version=APP_VERSION,
    )


@app.route("/banque")
def banque():
    from app.config import count_questions_for_cert, get_target_certification

    cert = get_target_certification()
    cert_counts = count_questions_for_cert(ALL_QUESTIONS, cert)
    return render_template_string(
        BANK_HTML,
        app_version=APP_VERSION,
        target_certification=cert,
        cert_question_count=cert_counts["matched"],
        total_bank=cert_counts["total_bank"],
    )


@app.route("/import-capture/preference", methods=["POST"])
def import_capture_preference():
    """Mémorise Udemy / Odoo (config.json) sans relancer l’analyse."""
    src = _normalize_capture_source(request.form.get("source") or "")
    _save_capture_source_preference(src)
    return jsonify({"ok": True, "source": src})


@app.route("/import-capture/fullpage", methods=["POST", "OPTIONS"])
def import_capture_fullpage_upload():
    """Réception image depuis le favori pleine page (Odoo / Udemy) — évite postMessage volumineux."""
    if request.method == "OPTIONS":
        return _fullpage_cors(Response("", status=204))

    upload = request.files.get("image")
    if not upload or not upload.filename:
        resp = jsonify({"error": "Aucune image reçue."})
        resp.status_code = 400
        return _fullpage_cors(resp)

    raw = upload.read()
    if len(raw) > FULLPAGE_MAX_BYTES:
        resp = jsonify({"error": "Image trop volumineuse (max 12 Mo)."})
        resp.status_code = 413
        return _fullpage_cors(resp)
    if len(raw) < 32:
        resp = jsonify({"error": "Fichier image invalide."})
        resp.status_code = 400
        return _fullpage_cors(resp)

    tid = uuid.uuid4().hex
    FULLPAGE_INBOX.mkdir(parents=True, exist_ok=True)
    _prune_stale_fullpage_inbox()
    page_host = (request.form.get("page_host") or "").strip()[:200]
    dom_raw = (request.form.get("dom_json") or "").strip()
    dom_stored = None
    if dom_raw:
        try:
            dom_stored = json.loads(dom_raw)
        except json.JSONDecodeError:
            dom_stored = None
    meta = {
        "source": _normalize_capture_source(
            request.form.get("source") or ""
        ),
        "mime": (upload.mimetype or "image/png").split(";")[0].strip() or "image/png",
        "page_host": page_host,
        "dom": dom_stored,
    }
    try:
        path = _fullpage_inbox_path(tid)
        path.write_bytes(raw)
        (FULLPAGE_INBOX / f"{tid}.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        resp = jsonify({"error": f"Enregistrement impossible : {e}"})
        resp.status_code = 500
        return _fullpage_cors(resp)

    resp = jsonify({"ok": True, "id": tid, "source": meta["source"]})
    return _fullpage_cors(resp)


@app.route("/import-capture/fullpage/<tid>/meta", methods=["GET"])
def import_capture_fullpage_meta(tid: str):
    """Métadonnées (DOM, hôte) avant téléchargement de l’image pleine page."""
    t = (tid or "").strip().lower()
    if not _valid_pending_token(t):
        return jsonify({"error": "Identifiant invalide."}), 400
    meta_path = FULLPAGE_INBOX / f"{t}.json"
    if not meta_path.is_file() or not _fullpage_inbox_path(t).is_file():
        return jsonify({"error": "Capture expirée ou introuvable."}), 404
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return jsonify({"error": "Métadonnées invalides."}), 500
    return jsonify(
        {
            "ok": True,
            "id": t,
            "source": meta.get("source") or "udemy",
            "page_host": meta.get("page_host") or "",
            "dom": meta.get("dom"),
            "mime": meta.get("mime") or "image/png",
        }
    )


@app.route("/import-capture/fullpage/<tid>/image", methods=["GET"])
def import_capture_fullpage_image(tid: str):
    """Image déposée par le favori pleine page."""
    t = (tid or "").strip().lower()
    if not _valid_pending_token(t):
        return jsonify({"error": "Identifiant invalide."}), 400
    path = _fullpage_inbox_path(t)
    if not path.is_file():
        return jsonify({"error": "Capture expirée ou introuvable."}), 404
    meta_path = FULLPAGE_INBOX / f"{t}.json"
    mime = "image/png"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            mime = (meta.get("mime") or mime).strip() or mime
        except (json.JSONDecodeError, OSError):
            pass
    try:
        return send_file(path, mimetype=mime, max_age=0)
    finally:
        try:
            path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
        except OSError:
            pass


@app.route("/import-capture/fullpage/<tid>", methods=["GET"])
def import_capture_fullpage_download(tid: str):
    """Rétrocompatibilité : renvoie l’image (préférer /meta puis /image)."""
    return import_capture_fullpage_image(tid)


@app.route("/import-capture/pipeline", methods=["POST"])
def import_capture_pipeline():
    """Flux NDJSON : vision Anthropic (image) → banque → doublons → enrichissement."""
    if not api_available():
        return jsonify({"error": "Clé API Anthropic absente (config.json → anthropic.api_key)."}), 400
    upload = request.files.get("screenshot")
    if not upload or not upload.filename:
        return jsonify({"error": "Aucune image reçue."}), 400

    name = secure_filename(upload.filename) or "capture"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_CAPTURE_EXT:
        return (
            jsonify(
                {
                    "error": f"Format non pris en charge ({ext or 'inconnu'}) — png, jpg, jpeg, gif ou webp."
                }
            ),
            400,
        )

    cap_src = _normalize_capture_source(request.form.get("source") or request.args.get("source"))
    page_host = (request.form.get("page_host") or "").strip()[:200]
    dom_payload = None
    dom_raw = (request.form.get("dom_json") or "").strip()
    if dom_raw:
        try:
            dom_payload = json.loads(dom_raw)
        except json.JSONDecodeError:
            dom_payload = None
    fd, tmp_path = tempfile.mkstemp(suffix="." + ext, prefix="quiz_cap_")
    os.close(fd)
    try:
        upload.save(tmp_path)
    except OSError as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return jsonify({"error": f"Impossible d’enregistrer l’image : {e}"}), 500

    def gen():
        try:
            yield from _iter_capture_pipeline_ndjson_from_path(
                tmp_path,
                ext,
                capture_source=cap_src,
                dom_payload=dom_payload,
                page_host=page_host,
            )
        finally:
            if tmp_path and os.path.isfile(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    return Response(
        stream_with_context(gen()),
        mimetype="application/x-ndjson; charset=utf-8",
    )


@app.route("/import-capture/preview", methods=["GET"])
def import_capture_preview():
    """Aperçu validation après analyse (données pending sur disque)."""
    if not api_available():
        return _render_capture(
            error="Clé API Anthropic absente (config.json → anthropic.api_key). "
            "Elle est nécessaire pour lire l’image."
        )
    pid = (request.args.get("pending") or "").strip().lower()
    loaded = _load_pending_capture(pid)
    if not loaded:
        return _render_capture(
            error="Aperçu expiré ou invalide. Collez à nouveau la capture et relancez l’analyse."
        )
    items, screen_path, meta = loaded
    preview_blocks = _build_preview_blocks_from_saved_items(items, screen_path)
    vision_notice = (meta or {}).get("vision_notice") or ""
    return render_template_string(
        CAPTURE_PREVIEW_HTML,
        app_version=APP_VERSION,
        pending_id=pid,
        preview_blocks=preview_blocks,
        vision_notice=vision_notice,
    )


@app.route("/import-capture", methods=["GET", "POST"])
def import_capture():
    if not api_available():
        return _render_capture(
            error="Clé API Anthropic absente (config.json → anthropic.api_key). "
            "Elle est nécessaire pour lire l’image."
        )

    if request.method == "GET":
        if request.args.get("cancel") == "1":
            pid = (request.args.get("pending") or "").strip().lower()
            if _valid_pending_token(pid):
                _clear_pending(pid)
            return redirect(url_for("import_capture"))
        success_msg = session.pop("capture_success", None) or ""
        notice_msg = session.pop("capture_notice", None) or ""
        explicit_src = request.args.get("source")
        if explicit_src:
            _save_capture_source_preference(explicit_src)
            default_src = explicit_src
        else:
            default_src = _load_capture_source_preference()
        return _render_capture(
            success=success_msg, notice=notice_msg, default_source=default_src
        )

    # POST
    if request.form.get("confirm") == "1":
        pid = (request.form.get("pending_id") or "").strip().lower()
        loaded = _load_pending_capture(pid)
        if not loaded:
            return _render_capture(
                error="Aperçu expiré ou invalide. Collez à nouveau la capture et relancez l’analyse."
            )
        items, screen_path, _meta = loaded
        try:
            submit_ix_raw = (request.form.get("submit_card_index") or "").strip()
            only_ix: Optional[int] = int(submit_ix_raw) if submit_ix_raw.isdigit() else None
            edited = _parse_capture_confirm_form(request.form, only_index=only_ix)
            res = apply_capture_items_to_bank(
                edited,
                screenshot_path=screen_path,
                use_claude_for_incomplete_new=True,
                verbose=False,
            )
            remaining_cards = 0
            if only_ix is not None:
                remaining = [items[j] for j in range(len(items)) if j != only_ix]
                if remaining:
                    _save_pending_items_list(pid, remaining)
                    remaining_cards = len(remaining)
                else:
                    _clear_pending(pid)
            else:
                _clear_pending(pid)
            reload_questions()
            added = int(res.get("added_count") or 0)
            updated = int(res.get("updated_count") or 0)
            skipped = res.get("skipped") or []
            img_up = res.get("images_updated") or []
            img_warn = res.get("image_warnings") or []
            bits = []
            if added:
                bits.append(f"{added} question(s) ajoutée(s).")
            if updated:
                bits.append(f"{updated} question(s) mise(s) à jour dans la banque.")
            if img_up:
                bits.append(f"{len(img_up)} image(s) de question mise(s) à jour.")
            if skipped:
                bits.append(f"{len(skipped)} carte(s) ignorée(s) (validation).")
            if not bits:
                bits.append("Aucun changement enregistré.")
            success = " ".join(bits)
            notice = ""
            if skipped:
                notice = "Détail ignorés : " + " ; ".join(skipped[:8])
                if len(skipped) > 8:
                    notice += " …"
            if img_warn:
                extra = "Images : " + " ; ".join(img_warn[:6])
                if len(img_warn) > 6:
                    extra += " …"
                notice = (notice + " " if notice else "") + extra
            session["capture_success"] = success
            session["capture_notice"] = notice
            if request.form.get("ajax") == "1" or request.headers.get("X-Capture-Ajax") == "1":
                return jsonify(
                    {
                        "ok": True,
                        "success": success,
                        "notice": notice,
                        "remaining_cards": remaining_cards,
                        "pending_id": pid if remaining_cards else None,
                    }
                )
            if remaining_cards:
                return redirect(
                    url_for("import_capture_preview", pending=pid, saved=1)
                )
            return redirect(url_for("import_capture"))
        except ValueError as e:
            if request.form.get("ajax") == "1" or request.headers.get("X-Capture-Ajax") == "1":
                return jsonify({"ok": False, "error": str(e)}), 400
            return _render_capture(error=str(e))
        except (RuntimeError, OSError) as e:
            if request.form.get("ajax") == "1" or request.headers.get("X-Capture-Ajax") == "1":
                return jsonify({"ok": False, "error": str(e)}), 500
            return _render_capture(error=str(e))
        except Exception as e:
            if request.form.get("ajax") == "1" or request.headers.get("X-Capture-Ajax") == "1":
                return jsonify({"ok": False, "error": f"Erreur : {e}"}), 500
            return _render_capture(error=f"Erreur : {e}")

    return (
        _render_capture(
            error="Envoi direct non pris en charge. Utilisez le bouton « Analyser la capture » sur cette page."
        ),
        400,
    )


@app.route("/api/odoo/surveys")
def api_odoo_surveys():
    """Liste les sondages (quiz) Odoo accessibles via XML-RPC."""
    search = (request.args.get("q") or "").strip()
    try:
        cfg = load_odoo_extract_config()
        from extract_odoo import connect

        db, api_key, uid, models = connect(cfg)
        rows = list_surveys(db, api_key, uid, models, search=search, limit=50)
        return jsonify({"ok": True, "surveys": rows, "odoo_url": cfg.get("odoo", {}).get("url", "")})
    except OdooExtractError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/odoo/extract", methods=["POST"])
def api_odoo_extract():
    """Extrait un sondage Odoo vers questions.json et recharge la banque."""
    data = request.get_json(silent=True) or {}
    survey_id = data.get("survey_id")
    survey_name = (data.get("survey_name") or "").strip() or None
    try:
        if survey_id is not None:
            survey_id = int(survey_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "survey_id invalide."}), 400

    try:
        summary = extract_survey_to_file(
            CFG,
            survey_id=survey_id,
            survey_name=survey_name,
            backup=True,
        )
        reload_questions()
        return jsonify(summary)
    except OdooExtractError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/settings/target_certification", methods=["GET", "PATCH"])
def api_settings_target_certification():
    from app.config import count_questions_for_cert, get_target_certification, set_target_certification

    if request.method == "GET":
        cert = get_target_certification()
        counts = count_questions_for_cert(ALL_QUESTIONS, cert)
        return jsonify(
            {
                "target_certification": cert,
                "cert_question_count": counts["matched"],
                "total_bank": counts["total_bank"],
                "by_target_version": counts["by_target_version"],
            }
        )
    payload = request.get_json(silent=True) or {}
    raw = payload.get("target_certification") or payload.get("value")
    if not raw:
        return jsonify({"error": "Champ target_certification ou value requis."}), 400
    try:
        cert = set_target_certification(str(raw).strip())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    counts = count_questions_for_cert(ALL_QUESTIONS, cert)
    return jsonify(
        {
            "ok": True,
            "target_certification": cert,
            "cert_question_count": counts["matched"],
            "total_bank": counts["total_bank"],
            "by_target_version": counts["by_target_version"],
        }
    )


@app.route("/api/bank")
def api_bank_list():
    import bank_topics as bt
    from app.config import count_questions_for_cert, filter_questions_for_cert, get_target_certification

    search = (request.args.get("q") or "").strip().lower()
    topic_filter = (request.args.get("topic") or "").strip()
    source_filter = (request.args.get("source") or "").strip().lower()
    version_filter = (request.args.get("version") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    tier_filter = (request.args.get("tier") or "").strip()
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    try:
        lim = min(500, max(5, int(request.args.get("limit", 40))))
    except ValueError:
        lim = 40
    cert = get_target_certification()
    cert_counts = count_questions_for_cert(ALL_QUESTIONS, cert)
    # Inférence des modules sur TOUT le corpus (contexte complet pour le kNN), caché.
    inferred = bt.infer_modules(ALL_QUESTIONS)
    sorted_q = _sort_questions_bank(filter_questions_for_cert(ALL_QUESTIONS, cert))
    topic_tree = bt.build_topic_tree(sorted_q, inferred)

    def _version_ok(q):
        if not version_filter:
            return True
        tv = (q.get("target_version") or "").strip()
        if version_filter == "both":
            return tv == "both"
        if version_filter == "none":
            return not tv
        if version_filter in ("18.0", "19.0"):
            return tv == version_filter or tv == "both"
        return True

    filtered_rows = []
    for num, q in enumerate(sorted_q, start=1):
        mod, mod_inf = bt.resolve_module(q, inferred)
        disp_label = bt.module_label(mod) if mod else bt.UNCLASSIFIED_LABEL
        cat_label = bt.category_label(bt.category_of(mod)) if mod else bt.UNCLASSIFIED_LABEL
        src = _normalized_answer_source(q)
        title = (q.get("title") or "").strip()
        if search:
            blob = (
                title + " " + (q.get("title_fr") or "") + " " + disp_label + " "
                + cat_label + " " + (mod or "") + " " + str(q.get("id"))
            ).lower()
            if search not in blob:
                continue
        if not bt.matches_topic_filter(q, inferred, topic_filter):
            continue
        if source_filter and (src or "") != source_filter:
            continue
        if not _version_ok(q):
            continue
        if status_filter and (q.get("status") or "") != status_filter:
            continue
        if tier_filter and (q.get("tier") or "") != tier_filter:
            continue
        tv = (q.get("target_version") or "").strip() or None
        filtered_rows.append(
            {
                "num": num,
                "id": q.get("id"),
                "title": title,
                "topic": disp_label,
                "category": cat_label,
                "module": mod or None,
                "topic_auto": mod_inf,
                "target_version": tv,
                "status": (q.get("status") or None),
                "tier": (q.get("tier") or None),
                "correct_answer_source": src,
                "has_question_image": bool(normalize_question_media_rel(q.get("question_image")))
                and has_valid_question_image(q),
            }
        )
    total = len(filtered_rows)
    page = filtered_rows[offset : offset + lim]
    return jsonify(
        {
            "items": page,
            "total": total,
            "topic_tree": topic_tree,
            "topics": [],  # déprécié (remplacé par topic_tree)
            "target_certification": cert,
            "cert_question_count": cert_counts["matched"],
            "total_bank": cert_counts["total_bank"],
        }
    )


@app.route("/api/bank/modules")
def api_bank_modules():
    """Catalogue complet catégorie → modules (pour le sélecteur de l'éditeur)."""
    import bank_topics as bt

    return jsonify({"catalog": bt.full_catalog()})


@app.route("/api/bank/<q_id>")
def api_bank_get(q_id):
    from app.config import filter_questions_for_cert, get_target_certification

    q_id = _parse_question_id_param(q_id)
    if q_id is None:
        return jsonify({"error": "Identifiant question invalide."}), 400
    cert = get_target_certification()
    sorted_q = _sort_questions_bank(filter_questions_for_cert(ALL_QUESTIONS, cert))
    num = None
    q = None
    for i, row in enumerate(sorted_q, start=1):
        try:
            if int(row.get("id")) == q_id:
                num = i
                q = row
                break
        except (TypeError, ValueError):
            continue
    if not q:
        if 1 <= q_id <= len(sorted_q):
            candidate = sorted_q[q_id - 1]
            try:
                real_id = int(candidate.get("id"))
            except (TypeError, ValueError):
                real_id = None
            if real_id is not None and real_id != q_id:
                return jsonify(
                    {
                        "error": (
                            f"Aucune question avec l'id {q_id}. "
                            f"Le n° #{q_id} dans la liste correspond à l'id {real_id}."
                        ),
                        "hint": "num_not_id",
                        "bank_id": real_id,
                        "num": q_id,
                    }
                ), 404
        return jsonify({"error": "Question introuvable."}), 404
    stored = (q.get("topic") or "").strip()
    inferred = _infer_odoo_topic(q)
    disp, is_auto = _display_topic(q)
    qi = normalize_question_media_rel(q.get("question_image"))
    import bank_topics as bt

    real_module = bt._norm_module(q.get("module"))
    inferred_map = bt.infer_modules(ALL_QUESTIONS)
    resolved_module, module_is_inferred = bt.resolve_module(q, inferred_map)
    return jsonify(
        {
            "num": num,
            "id": q_id,
            "title": q.get("title") or "",
            "title_fr": q.get("title_fr") or "",
            "topic": stored,
            "topic_inferred": inferred,
            "topic_display": disp,
            "topic_is_auto_display": is_auto and not stored,
            "module": real_module or None,
            "module_resolved": resolved_module or None,
            "module_is_inferred": module_is_inferred,
            "module_label": bt.module_label(resolved_module) if resolved_module else None,
            "answers": copy.deepcopy(q.get("answers") or []),
            "explication_senedoo": q.get("explication_senedoo") or "",
            "explication_claude": q.get("explication_claude") or "",
            "correct_answer_source": _normalized_answer_source(q),
            "target_version": (q.get("target_version") or "").strip() or None,
            "question_image": qi,
            "question_image_file_ok": bool(qi) and has_valid_question_image(q),
        }
    )


@app.route("/api/bank/<q_id>", methods=["PUT"])
def api_bank_put(q_id):
    q_id = _parse_question_id_param(q_id)
    if q_id is None:
        return jsonify({"error": "Identifiant question invalide."}), 400
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON attendu."}), 400
    data = _load_questions_file_raw()
    if not data or not isinstance(data.get("questions"), list):
        return jsonify({"error": "Fichier questions introuvable ou invalide."}), 500
    idx = _find_question_index(data, q_id)
    if idx is None:
        return jsonify({"error": "Question introuvable."}), 404
    old = data["questions"][idx]
    old_ci = _correct_1based_from_answers(old.get("answers"))
    prev_src = old.get("correct_answer_source")
    if prev_src not in ("udemy", "odoo", "claude", "user"):
        prev_src = None
    title = (payload.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Titre (EN) obligatoire."}), 400
    try:
        gmax = _max_answer_id(data["questions"])
        new_answers = _bank_put_answers(old, payload.get("answers") or [], gmax)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    new_ci = _correct_1based_from_answers(new_answers)
    merged = dict(old)
    merged["title"] = title
    merged["title_fr"] = (payload.get("title_fr") or "").strip()
    if "topic" in payload:
        merged["topic"] = (payload.get("topic") or "").strip()
    if "module" in payload:
        import bank_topics as bt

        new_mod = bt._norm_module(payload.get("module"))
        merged["module"] = new_mod or None
    merged["answers"] = new_answers
    merged["explication_senedoo"] = (payload.get("explication_senedoo") or "").strip()
    merged["explication_claude"] = (payload.get("explication_claude") or "").strip()
    if "target_version" in payload:
        from app.doc_schema import normalize_target_version

        raw_tv = payload.get("target_version")
        if raw_tv is None or (isinstance(raw_tv, str) and not str(raw_tv).strip()):
            merged["target_version"] = None
        else:
            merged["target_version"] = normalize_target_version(str(raw_tv).strip())
    if new_ci != old_ci:
        merged["correct_answer_source"] = "user"
    elif new_ci is not None:
        merged["correct_answer_source"] = prev_src or "udemy"
    else:
        merged.pop("correct_answer_source", None)
    data["questions"][idx] = merged
    _save_questions_file_raw(data)
    reload_questions()
    return jsonify({"ok": True})


@app.route("/api/bank/<q_id>/flag", methods=["POST"])
def api_bank_flag(q_id):
    """Signaler une question comme erronée → status=flagged (exclue du quiz).

    Endpoint public, déclenché depuis le bouton « Signaler » de l'UI quiz.
    Conserve l'ancien status dans ``prev_status`` pour permettre une
    revalidation depuis la page d'administration (/admin/review).
    """
    from datetime import datetime, timezone

    q_id = _parse_question_id_param(q_id)
    if q_id is None:
        return jsonify({"error": "Identifiant question invalide."}), 400
    payload = request.get_json(silent=True) or {}
    reason = (payload.get("reason") or "").strip()[:500]
    data = _load_questions_file_raw()
    if not data or not isinstance(data.get("questions"), list):
        return jsonify({"error": "Fichier questions introuvable ou invalide."}), 500
    idx = _find_question_index(data, q_id)
    if idx is None:
        return jsonify({"error": "Question introuvable."}), 404
    q = data["questions"][idx]
    if (q.get("status") or "") != "flagged":
        q["prev_status"] = q.get("status")
    q["status"] = "flagged"
    q["flag_reason"] = reason or q.get("flag_reason") or None
    q["flagged_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["questions"][idx] = q
    _save_questions_file_raw(data)
    reload_questions()
    return jsonify({"ok": True, "status": "flagged"})


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """Q&A libre via API Anthropic.

    Remplace l'ancien appel `subprocess.run(["claude", "-p", ...])` (qui dépendait
    du CLI Claude Code installé localement) par un appel direct au SDK Python.
    Cohérent avec /api/suggest-answer : même clé, même modèle (config.json).
    """
    data = request.get_json() or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Question vide"}), 400

    from app.llm import api_available, _anthropic_key, _answer_model, extract_text_from_content
    if not api_available():
        return jsonify({"error": "Clé API Anthropic absente dans config.json."}), 500

    system = (
        "Tu es expert certifié Odoo. Réponds en français à la question de "
        "façon claire et pédagogique (8-12 lignes max)."
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=_anthropic_key())
        resp = client.messages.create(
            model=_answer_model(),
            max_tokens=800,
            system=system,
            messages=[{"role": "user", "content": question}],
        )
        return jsonify({"answer": extract_text_from_content(resp.content).strip()})
    except anthropic.APIConnectionError as e:
        return jsonify({"error": f"Connexion API Anthropic impossible : {e}"}), 502
    except anthropic.RateLimitError:
        return jsonify({"error": "Quota API Anthropic dépassé."}), 429
    except anthropic.APIError as e:
        return jsonify({"error": f"Erreur API Anthropic : {e}"}), 500


@app.route("/api/questions")
def api_questions():
    from app.config import filter_questions_for_cert, get_target_certification

    module = (request.args.get("module") or "").strip() or None
    include_hidden = (request.args.get("include_hidden") or "").lower() in ("1", "true", "yes")
    pool = filter_questions_for_cert(
        ALL_QUESTIONS, module=module, include_hidden=include_hidden,
    )
    if not pool:
        return jsonify([])
    n = min(int(request.args.get("n", 20)), len(pool))
    sample = random.sample(pool, n) if n <= len(pool) else pool[:]
    return jsonify(sample)


@app.route("/api/modules")
def api_modules():
    """Liste des modules avec leur compte de questions disponibles
    (filtrage cert version + status non-hidden par défaut).

    Query :
      - cert (optionnel) : 18.0 ou 19.0 ; défaut = target_certification courante.
      - include_hidden=1 : inclure les unverified/flagged dans le compte.
    """
    from app.config import get_target_certification, list_modules_with_counts, normalize_cert_version

    raw_cert = request.args.get("cert")
    try:
        cert = normalize_cert_version(raw_cert) if raw_cert else get_target_certification()
    except ValueError:
        cert = get_target_certification()
    include_hidden = (request.args.get("include_hidden") or "").lower() in ("1", "true", "yes")
    modules = list_modules_with_counts(ALL_QUESTIONS, cert, include_hidden=include_hidden)
    return jsonify({
        "cert_version": cert,
        "include_hidden": include_hidden,
        "modules": modules,
    })


# ---------------------------------------------------------------------------
# Administration — revue des questions (unverified / flagged)
# ---------------------------------------------------------------------------

REVIEWABLE_STATUSES = ("unverified", "flagged")


def _admin_token() -> Optional[str]:
    """Jeton admin lu dans config.json -> admin.token (None si non configuré)."""
    tok = (CFG.get("admin") or {}).get("token")
    tok = str(tok).strip() if tok else ""
    return tok or None


def _admin_request_token() -> str:
    return (
        request.args.get("token")
        or request.headers.get("X-Admin-Token")
        or request.cookies.get("admin_token")
        or ""
    ).strip()


def _admin_authorized() -> bool:
    expected = _admin_token()
    if not expected:
        # Aucun jeton configuré : la page admin s'appuie sur la protection
        # globale de l'appli (login HTTP au niveau du reverse-proxy Caddy).
        # Accès ouvert aux utilisateurs déjà authentifiés sur l'appli.
        return True
    return _admin_request_token() == expected


@app.route("/admin/review")
def admin_review_page():
    expected = _admin_token()
    # Si un jeton admin est explicitement configuré, on l'exige. Sinon, la page
    # s'appuie sur la protection globale de l'appli (login Caddy).
    if expected and not _admin_authorized():
        return render_template_string(ADMIN_GATE_HTML, configured=True), 403
    resp = Response(render_template_string(ADMIN_HTML, app_version=APP_VERSION))
    if expected:
        # Mémorise le jeton (cookie httponly) pour les appels API ultérieurs.
        resp.set_cookie(
            "admin_token", _admin_request_token(),
            max_age=86400, httponly=True, samesite="Lax",
        )
    return resp


@app.route("/api/admin/review")
def api_admin_review():
    if not _admin_authorized():
        return jsonify({"error": "Non autorisé."}), 403
    from app.config import question_module

    want = (request.args.get("status") or "all").strip().lower()
    if want not in ("all", "unverified", "flagged"):
        want = "all"
    out = []
    counts = {"unverified": 0, "flagged": 0}
    for q in ALL_QUESTIONS:
        st = (q.get("status") or "").strip()
        if st in counts:
            counts[st] += 1
        if st not in REVIEWABLE_STATUSES:
            continue
        if want != "all" and st != want:
            continue
        out.append({
            "id": q.get("id"),
            "title": q.get("title"),
            "title_fr": q.get("title_fr"),
            "module": question_module(q),
            "status": st,
            "judge_score": q.get("judge_score"),
            "judge_reasons": q.get("judge_reasons"),
            "target_version": q.get("target_version"),
            "tier": q.get("tier"),
            "source": q.get("correct_answer_source") or q.get("source"),
            "flag_reason": q.get("flag_reason"),
            "explication_claude": q.get("explication_claude"),
            "explication_senedoo": q.get("explication_senedoo"),
            "answers": [
                {
                    "id": a.get("id"),
                    "value": a.get("value"),
                    "value_fr": a.get("value_fr"),
                    "is_correct": bool(a.get("is_correct")),
                }
                for a in (q.get("answers") or [])
            ],
        })
    out.sort(key=lambda r: (
        0 if r["status"] == "flagged" else 1,
        r["id"] if isinstance(r["id"], int) else 0,
    ))
    return jsonify({"questions": out, "counts": counts})


@app.route("/api/admin/questions/<q_id>/validate", methods=["POST"])
def api_admin_validate(q_id):
    if not _admin_authorized():
        return jsonify({"error": "Non autorisé."}), 403
    from datetime import datetime, timezone

    q_id = _parse_question_id_param(q_id)
    if q_id is None:
        return jsonify({"error": "Identifiant question invalide."}), 400
    data = _load_questions_file_raw()
    if not data or not isinstance(data.get("questions"), list):
        return jsonify({"error": "Fichier questions introuvable ou invalide."}), 500
    idx = _find_question_index(data, q_id)
    if idx is None:
        return jsonify({"error": "Question introuvable."}), 404
    q = data["questions"][idx]
    q["status"] = "verified_by_admin"
    q.pop("flag_reason", None)
    q.pop("flagged_at", None)
    q.pop("prev_status", None)
    q["validated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["questions"][idx] = q
    _save_questions_file_raw(data)
    reload_questions()
    return jsonify({"ok": True, "status": "verified_by_admin"})


@app.route("/api/admin/questions/<q_id>", methods=["DELETE"])
def api_admin_delete(q_id):
    if not _admin_authorized():
        return jsonify({"error": "Non autorisé."}), 403
    q_id = _parse_question_id_param(q_id)
    if q_id is None:
        return jsonify({"error": "Identifiant question invalide."}), 400
    data = _load_questions_file_raw()
    if not data or not isinstance(data.get("questions"), list):
        return jsonify({"error": "Fichier questions introuvable ou invalide."}), 500
    idx = _find_question_index(data, q_id)
    if idx is None:
        return jsonify({"error": "Question introuvable."}), 404
    removed = data["questions"].pop(idx)
    _save_questions_file_raw(data)
    reload_questions()
    return jsonify({"ok": True, "deleted_id": removed.get("id")})


@app.route("/health")
def health():
    """Vérifie que le serveur répond (utile pour scripts / débogage « inaccessible »)."""
    payload: dict[str, Any] = {
        "status": "ok",
        "version": APP_VERSION,
        "questions": len(ALL_QUESTIONS),
    }
    if request.args.get("anthropic_status") in ("1", "true", "yes"):
        from quiz_llm import anthropic_public_api_status

        payload["anthropic_public_status"] = anthropic_public_api_status(timeout=4.0)
    return jsonify(payload)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    flask_cfg = CFG.get("flask", {})
    if not ALL_QUESTIONS:
        print("⚠️  Aucune question chargée. Lance d'abord extract_odoo.py.")
    else:
        with_claude  = sum(1 for q in ALL_QUESTIONS if q.get("explication_claude"))
        with_senedoo = sum(1 for q in ALL_QUESTIONS if q.get("explication_senedoo"))
        print(f"✅ {len(ALL_QUESTIONS)} questions — {with_claude} explications Claude, {with_senedoo} Senedoo.")
    print(f"📌 Version app : {APP_VERSION}")
    host = str(flask_cfg.get("host", "127.0.0.1"))
    port = int(flask_cfg.get("port", 5001))
    loopback = "127.0.0.1" if host in ("0.0.0.0", "::", "[::]") else host
    print(f"🌐 Quiz : http://{loopback}:{port}/")
    print(f"🌐 Banque : http://{loopback}:{port}/banque")
    print(f"🌐 Capture quiz : http://{loopback}:{port}/import-capture")
    print(f"🌐 Santé : http://{loopback}:{port}/health (statut Anthropic public : ?anthropic_status=1)")
    if host == "0.0.0.0":
        print("   (écoute sur toutes les interfaces — depuis un autre appareil : http://<IP-de-ce-mac>:" + str(port) + "/ )")
    app.run(
        host=host,
        port=port,
        debug=flask_cfg.get("debug", False)
    )
