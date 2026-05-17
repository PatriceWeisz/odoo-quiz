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
from import_screenshot import CaptureNoQuizContentError, vision_extract_items_from_capture
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
APP_VERSION = "1.12.91"
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
           flex-wrap: wrap; gap: .5rem; }
  .header-nav { display: flex; gap: .75rem; align-items: center; flex-wrap: wrap; }
  header .title-block { display: flex; flex-direction: column; align-items: flex-start; gap: .1rem; }
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
                color: white; border-radius: 8px; padding: .4rem .9rem; cursor: pointer;
                font-size: .85rem; font-weight: 600; transition: .15s; }
  .header-btn:hover { background: rgba(255,255,255,.25); }
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
    <a class="header-btn" href="/" title="Accueil quiz" aria-current="page">🎓 Quiz</a>
    <a class="header-btn" href="/banque">📋 Banque</a>
    <a class="header-btn" href="/import-capture">📷 Capture</a>
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
      <a href="/import-capture?source=odoo" style="color:#714B67;font-weight:600">📷 Capture quiz (Udemy / Odoo)</a>
      <span style="color:#64748b"> — importer une question depuis une capture (API Anthropic)</span>
    </p>
  </div>

  <div id="question-card" style="display:none">
    <div id="progress">
      <span id="progress-text"></span>
      <button class="lang-toggle" id="lang-btn" onclick="toggleLang()" title="Afficher la traduction française">🇫🇷 FR</button>
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
const total = {{ total }};
const withClaude  = {{ with_claude }};
const withSenedoo = {{ with_senedoo }};

document.getElementById('start-info').innerHTML =
  `${total} questions disponibles.<br>` +
  `💡 ${withClaude} explications Claude · 📚 ${withSenedoo} explications Senedoo/Udemy<br>` +
  `Scoring : +1 bonne / −1 mauvaise / 0 saut.`;

async function startQuiz() {
  const count = Math.min(parseInt(document.getElementById('q-count').value) || 20, total);
  const res = await fetch(`/api/questions?n=${count}`);
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

ALLOWED_CAPTURE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
PENDING_CAPTURE_ROOT = Path(tempfile.gettempdir()) / "odoo_quiz_capture_pending"


def _valid_pending_token(tid: str) -> bool:
    t = (tid or "").strip().lower()
    return len(t) == 32 and all(c in "0123456789abcdef" for c in t)


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
        raw_correct = (form.get(f"{p}correct") or "").strip()
        if raw_correct.lower() in ("none", "null"):
            raw_correct = ""
        if not raw_correct.isdigit():
            raise ValueError(
                f"Carte {i + 1} : choisissez la bonne réponse (une option entre 1 et {n_ans})."
            )
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
    tmp_path: str, ext: str, capture_source: str = "udemy"
) -> Iterator[bytes]:
    """Événements NDJSON ; l’image doit déjà être sur disque (évite fichier upload fermé en streaming)."""
    cap_src = _normalize_capture_source(capture_source)
    vision_label = "Odoo (site / eLearning)" if cap_src == "odoo" else "Udemy"
    try:
        yield _capture_progress_line(
            "vision",
            "running",
            "1 · Vision (Claude)",
            f"Lecture de la capture {vision_label} via l’API Anthropic (modèle vision).",
        )

        vision_notice = ""
        try:
            items, cap_src, auto_odoo = vision_extract_items_from_capture(tmp_path, cap_src)
            if auto_odoo:
                _save_capture_source_preference("odoo")
                yield _capture_progress_line(
                    "vision",
                    "warn",
                    "Mode Odoo appliqué",
                    "Capture multi-questions détectée : analyse relancée en mode Odoo (une fiche par question).",
                    code="auto_odoo",
                )
            n_vision = len(items)
            if n_vision > 1:
                vision_done_detail = (
                    f"{n_vision} questions distinctes extraites de la même capture "
                    f"(ordre haut → bas). Une fiche de validation par question."
                )
            else:
                vision_done_detail = "Texte extrait ; suite : banque puis complément."
            yield _capture_progress_line(
                "vision",
                "done",
                "1 · Vision terminée",
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
    with_claude   = sum(1 for q in ALL_QUESTIONS if q.get("explication_claude"))
    with_senedoo  = sum(1 for q in ALL_QUESTIONS if q.get("explication_senedoo"))
    return render_template_string(HTML, total=len(ALL_QUESTIONS),
                                  with_claude=with_claude, with_senedoo=with_senedoo,
                                  app_version=APP_VERSION)


@app.route("/banque")
def banque():
    return render_template_string(BANK_HTML, app_version=APP_VERSION)


@app.route("/import-capture/preference", methods=["POST"])
def import_capture_preference():
    """Mémorise Udemy / Odoo (config.json) sans relancer l’analyse."""
    src = _normalize_capture_source(request.form.get("source") or "")
    _save_capture_source_preference(src)
    return jsonify({"ok": True, "source": src})


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
    _save_capture_source_preference(cap_src)
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
            yield from _iter_capture_pipeline_ndjson_from_path(tmp_path, ext, capture_source=cap_src)
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


@app.route("/api/bank")
def api_bank_list():
    search = (request.args.get("q") or "").strip().lower()
    topic_filter = (request.args.get("topic") or "").strip()
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    try:
        lim = min(500, max(5, int(request.args.get("limit", 40))))
    except ValueError:
        lim = 40
    sorted_q = _sort_questions_bank(ALL_QUESTIONS)
    topics_set = set()
    for q in sorted_q:
        disp, _ = _display_topic(q)
        topics_set.add(disp)
    filtered_rows = []
    for num, q in enumerate(sorted_q, start=1):
        disp, inferred = _display_topic(q)
        title = (q.get("title") or "").strip()
        if search:
            blob = (title + " " + (q.get("title_fr") or "") + " " + disp + " " + str(q.get("id"))).lower()
            if search not in blob:
                continue
        if topic_filter and topic_filter != "__all__" and disp != topic_filter:
            continue
        filtered_rows.append(
            {
                "num": num,
                "id": q.get("id"),
                "title": title,
                "topic": disp,
                "topic_auto": inferred,
                "correct_answer_source": _normalized_answer_source(q),
                "has_question_image": bool(normalize_question_media_rel(q.get("question_image")))
                and has_valid_question_image(q),
            }
        )
    total = len(filtered_rows)
    page = filtered_rows[offset : offset + lim]
    return jsonify({"items": page, "total": total, "topics": sorted(topics_set)})


@app.route("/api/bank/<int:q_id>")
def api_bank_get(q_id):
    sorted_q = _sort_questions_bank(ALL_QUESTIONS)
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
        return jsonify({"error": "Question introuvable."}), 404
    stored = (q.get("topic") or "").strip()
    inferred = _infer_odoo_topic(q)
    disp, is_auto = _display_topic(q)
    qi = normalize_question_media_rel(q.get("question_image"))
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
            "answers": copy.deepcopy(q.get("answers") or []),
            "explication_senedoo": q.get("explication_senedoo") or "",
            "explication_claude": q.get("explication_claude") or "",
            "correct_answer_source": _normalized_answer_source(q),
            "question_image": qi,
            "question_image_file_ok": bool(qi) and has_valid_question_image(q),
        }
    )


@app.route("/api/bank/<int:q_id>", methods=["PUT"])
def api_bank_put(q_id):
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
    merged["topic"] = (payload.get("topic") or "").strip()
    merged["answers"] = new_answers
    merged["explication_senedoo"] = (payload.get("explication_senedoo") or "").strip()
    merged["explication_claude"] = (payload.get("explication_claude") or "").strip()
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


@app.route("/api/ask", methods=["POST"])
def api_ask():
    import subprocess
    data = request.get_json()
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Question vide"}), 400
    prompt = (
        f"Tu es expert certifié Odoo. Réponds en français à cette question "
        f"de façon claire et pédagogique (8-12 lignes max) :\n\n{question}"
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip()}), 500
        return jsonify({"answer": result.stdout.strip()})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout — Claude n'a pas répondu à temps."}), 504
    except FileNotFoundError:
        return jsonify({"error": "CLI claude introuvable."}), 500


@app.route("/api/questions")
def api_questions():
    n = min(int(request.args.get("n", 20)), len(ALL_QUESTIONS))
    sample = random.sample(ALL_QUESTIONS, n) if n <= len(ALL_QUESTIONS) else ALL_QUESTIONS[:]
    return jsonify(sample)


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
