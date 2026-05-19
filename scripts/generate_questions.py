#!/usr/bin/env python3
"""Générateur de questions QCM via Claude — Phase 5.4 (mini-run) & 5.5 (full).

Pour chaque chunk doc Odoo sélectionné, demande à Claude (Sonnet 4.6) de
produire N questions QCM bilingues structurées. Sauve les questions valides
dans `data/generated_pending/<batch_id>.jsonl` SANS toucher à questions.json
(c'est Phase 5.6 qui fait l'insertion atomique après validation).

Mode mini-run (Phase 5.4) — par défaut :
  - 50 questions sur 1 module + 1 version (ex: inventory_and_mrp/inventory v19)
  - Mode synchrone (1 req Claude à la fois) — pas Batch API
  - Coût attendu ~$0.50

Usage :
  python3 -m scripts.generate_questions                                   # 50 q, inventory v19
  python3 -m scripts.generate_questions --module sales --version 19.0
  python3 -m scripts.generate_questions --count 50 --per-call 4
  python3 -m scripts.generate_questions --dry-run                         # liste chunks, n'appelle pas Claude
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm import _anthropic_key, _answer_model  # noqa: E402
from app.odoo_docs_rag import db_path  # noqa: E402
from app.question_schema import (  # noqa: E402
    new_generated_question,
    now_iso,
    validate_generated_question,
)
from app.study_modules import tier_of, url_paths_for  # noqa: E402

DEFAULT_MODULE = "inventory_and_mrp/inventory"
DEFAULT_VERSION = "19.0"
DEFAULT_COUNT = 50
DEFAULT_PER_CALL = 4
DEFAULT_CONCURRENCY = 20
PENDING_DIR = ROOT / "data" / "generated_pending"
QUESTIONS_FILE = ROOT / "questions.json"
MIN_CHUNK_CHARS = 400
MAX_CHUNK_CHARS = 4500

SYSTEM_PROMPT = """Tu es un expert Odoo qui rédige des questions QCM pour la certification fonctionnelle Odoo (versions 18 et 19).

Tu produis EXCLUSIVEMENT du JSON strict, aucun texte hors JSON, aucune balise markdown.

Règles non négociables pour CHAQUE question :
- titre en anglais (10-25 mots typiques, jamais > 50)
- titre traduit en français
- 3 OU 4 options (alterner ; choisis 3 si c'est court, 4 si c'est nuancé)
- EXACTEMENT 1 option avec is_correct=true
- distracteurs PLAUSIBLES (concepts proches mais incorrects)
- la bonne réponse NE doit PAS être plus longue ni plus détaillée que les distracteurs (sinon triche par longueur)
- PAS d'options "All of the above" / "None of the above" / "Toutes les réponses"
- niveau cert FONCTIONNELLE : compréhension métier/UI Odoo, pas développement
- **evidence_snippet** : OBLIGATOIREMENT entre **50 et 150 mots** d'extrait textuel pris **MOT POUR MOT** dans le chunk fourni. C'est le passage qui prouve que la bonne réponse est juste. Inclus 2-3 phrases complètes contextuelles, pas seulement la phrase-clé isolée. Une question avec un evidence_snippet < 50 mots est INVALIDE — recommence si tu n'en trouves pas assez (élargis le contexte autour du passage clé).
- explication courte (2-4 phrases) en français expliquant POURQUOI la bonne réponse est juste

Format strict — array JSON de N questions :
[
  {
    "title": "...",
    "title_fr": "...",
    "options": [
      {"value": "...", "value_fr": "...", "is_correct": true},
      {"value": "...", "value_fr": "...", "is_correct": false},
      ...
    ],
    "difficulty": "facile" | "moyen" | "difficile",
    "scenario_based": true | false,
    "evidence_snippet": "...",
    "explication_claude": "..."
  }
]
"""


# --- Sélection des chunks ----------------------------------------------------


def _module_like_patterns(module: str) -> list[str]:
    out = []
    for path in url_paths_for(module):
        out.append(f"%/applications/{path}/%")
        out.append(f"%/applications/{path}.html%")
    return out


def select_chunks(
    conn: sqlite3.Connection,
    *,
    version: str,
    module: str,
    n: int,
    seed: int | None = None,
) -> list[dict]:
    """Tire aléatoirement n chunks (taille 400-4500 chars) du module+version."""
    patterns = _module_like_patterns(module)
    sql = (
        "SELECT chunk_id, url, title, section, text FROM chunks "
        f"WHERE version = ? AND ({' OR '.join('url LIKE ?' for _ in patterns)}) "
        "AND length(text) BETWEEN ? AND ?"
    )
    rows = conn.execute(
        sql, (version, *patterns, MIN_CHUNK_CHARS, MAX_CHUNK_CHARS)
    ).fetchall()
    rng = random.Random(seed)
    rng.shuffle(rows)
    rows = rows[:n]
    return [
        {
            "chunk_id": r[0],
            "url": r[1],
            "title": r[2] or "",
            "section": r[3] or "",
            "text": r[4] or "",
        }
        for r in rows
    ]


# --- Few-shot from existing Udemy ---------------------------------------------


def load_few_shot_pool() -> list[dict]:
    if not QUESTIONS_FILE.exists():
        return []
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    udemy = [
        q for q in data.get("questions", [])
        if isinstance(q, dict)
        and q.get("correct_answer_source") == "udemy"
        and len(q.get("answers") or []) in (3, 4)
        and sum(1 for a in q.get("answers") or [] if a.get("is_correct")) == 1
    ]
    return udemy


def pick_few_shot(pool: list[dict], k: int = 3, seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    return rng.sample(pool, min(k, len(pool)))


def format_few_shot(qs: list[dict]) -> str:
    """Compact text presentation pour le prompt."""
    lines = []
    for i, q in enumerate(qs, 1):
        title = (q.get("title") or "").strip()
        opts = q.get("answers") or []
        lines.append(f"Example {i}: {title}")
        for a in opts:
            marker = "→" if a.get("is_correct") else "·"
            lines.append(f"  {marker} {a.get('value','').strip()}")
        lines.append("")
    return "\n".join(lines)


# --- LLM call ----------------------------------------------------------------


def _build_user_prompt(
    *,
    chunk: dict,
    module: str,
    version: str,
    per_call: int,
    few_shot_text: str,
) -> str:
    return f"""Module Odoo : `{module}` — Version : {version}
URL : {chunk['url']}
Titre de page : {chunk['title']}
Section : {chunk['section']}

--- TEXTE DU CHUNK DOC OFFICIELLE ---
{chunk['text']}
--- FIN ---

Exemples de questions de certification réelles (style/ton à reproduire) :
{few_shot_text}

Tâche : produis {per_call} questions QCM distinctes basées sur le texte du chunk ci-dessus.
Réponds avec un array JSON de {per_call} questions, conforme au format imposé. Pas de texte hors JSON."""


def _system_blocks(system: str) -> list[dict]:
    """System prompt avec cache_control ephemeral pour bénéficier du prompt
    caching d'Anthropic (utile sur N appels avec même system).

    Si le system fait < 1024 tokens, le caching est silencieusement ignoré
    par l'API — pas d'erreur.
    """
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def call_claude(client, *, system: str, user: str, model: str, max_tokens: int = 4096) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_system_blocks(system),
        messages=[{"role": "user", "content": user}],
    )
    parts = []
    for block in resp.content or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


async def call_claude_async(aclient, *, system: str, user: str, model: str, max_tokens: int = 4096) -> str:
    """Variante async — utilisée par le mode parallélisé avec semaphore."""
    resp = await aclient.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_system_blocks(system),
        messages=[{"role": "user", "content": user}],
    )
    parts = []
    for block in resp.content or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


def _parse_json_array(text: str) -> list:
    """Extrait un array JSON depuis la réponse Claude (résiste aux balises md)."""
    # Strip code fences
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # Locate the array
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e == -1 or e <= s:
        raise ValueError("Pas d'array JSON détecté dans la réponse Claude")
    return json.loads(text[s:e + 1])


# --- Construction de la question finale ---------------------------------------


def _max_answer_id(bank_questions: list[dict]) -> int:
    out = 0
    for q in bank_questions:
        for a in (q.get("answers") or []):
            aid = a.get("id")
            if isinstance(aid, int) and aid > out:
                out = aid
    return out


def _next_question_id(bank_questions: list[dict]) -> int:
    return max((int(q.get("id") or 0) for q in bank_questions), default=0) + 1


def assemble_question(
    *,
    qid: int,
    answer_id_start: int,
    raw_q: dict,
    chunk: dict,
    module: str,
    tier: str,
    version: str,
) -> tuple[dict, int]:
    """Convertit le JSON Claude en question conforme au schéma banque."""
    options_raw = raw_q.get("options") or []
    answers = []
    aid = answer_id_start
    for opt in options_raw:
        answers.append({
            "id": aid,
            "value": (opt.get("value") or "").strip(),
            "value_fr": (opt.get("value_fr") or "").strip(),
            "is_correct": bool(opt.get("is_correct")),
            "score": 0.0,
        })
        aid += 1

    target_version = "19.0" if version == "19.0" else "18.0"
    q = new_generated_question(
        qid=qid,
        title=(raw_q.get("title") or "").strip(),
        title_fr=(raw_q.get("title_fr") or "").strip(),
        answers=answers,
        module=module,
        tier=tier,
        difficulty=(raw_q.get("difficulty") or "moyen").strip(),
        scenario_based=bool(raw_q.get("scenario_based")),
        target_version=target_version,
        source_chunk_id=chunk["chunk_id"],
        source_chunk_url=chunk["url"],
        evidence_snippet=(raw_q.get("evidence_snippet") or "").strip(),
        explication_claude=(raw_q.get("explication_claude") or "").strip(),
    )
    return q, aid


# --- Run principal -----------------------------------------------------------


def _setup_run(
    *,
    module: str,
    version: str,
    count: int,
    per_call: int,
    output_path: Path | None,
    seed: int | None,
    dry_run: bool,
) -> tuple[list[dict], list[dict], int, int, Path, str, str] | int:
    """Setup commun pour run sync ou async.

    Retourne (chunks, fewshot_pool, next_qid, next_aid, output_path, tier, model)
    ou un int (exit code) si erreur ou dry-run.
    """
    tier = tier_of(module) or "?"
    if tier == "?":
        print(f"⚠️  module {module!r} non listé dans STUDY_MODULES — tag tier='?'",
              file=sys.stderr)

    db = db_path()
    if not db.exists():
        print(f"❌ DB doc Odoo introuvable : {db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(db)

    n_chunks_needed = math.ceil(count / per_call)
    chunks = select_chunks(
        conn, version=version, module=module, n=n_chunks_needed, seed=seed,
    )
    conn.close()
    if not chunks:
        print(f"❌ Aucun chunk trouvé pour module={module} version={version}",
              file=sys.stderr)
        return 1
    print(f"→ Sélectionnés : {len(chunks)} chunks pour ~{count} questions "
          f"(per_call={per_call}, module={module}, version={version})")

    if dry_run:
        for c in chunks:
            print(f"  - {c['chunk_id']} | {len(c['text'])} chars | {c['url']}")
        return 0

    pool = load_few_shot_pool()
    if not pool:
        print("⚠️  Aucun few-shot Udemy disponible — pool vide", file=sys.stderr)

    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        bank_data = json.load(f)
    bank_qs = bank_data.get("questions") or []
    next_qid = _next_question_id(bank_qs)
    next_aid = _max_answer_id(bank_qs) + 1

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_path or (PENDING_DIR / f"{module.replace('/', '__')}-v{version}-{batch_id}.jsonl")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    model = _answer_model()
    print(f"→ Modèle : {model}")
    print(f"→ Sortie : {output_path}\n")
    return chunks, pool, next_qid, next_aid, output_path, tier, model


def _process_raw_questions(
    *,
    arr: list[dict],
    chunk: dict,
    module: str,
    tier: str,
    version: str,
    next_qid: int,
    next_aid: int,
    count_remaining: int,
) -> tuple[list[dict], int, int, int, list[str]]:
    """Assemble + valide les questions brutes Claude. Retourne :
       (questions_valides, new_next_qid, new_next_aid, n_invalid, errors).
    """
    out: list[dict] = []
    errors: list[str] = []
    n_invalid = 0
    for raw_q in arr:
        if len(out) >= count_remaining:
            break
        try:
            q, next_aid = assemble_question(
                qid=next_qid, answer_id_start=next_aid,
                raw_q=raw_q, chunk=chunk, module=module,
                tier=tier, version=version,
            )
        except Exception as e:
            n_invalid += 1
            errors.append(f"assemble: {e}")
            continue
        errs = validate_generated_question(q)
        if errs:
            n_invalid += 1
            errors.append(f"qid={next_qid}: " + "; ".join(errs))
            continue
        out.append(q)
        next_qid += 1
    return out, next_qid, next_aid, n_invalid, errors


def run_generation(
    *,
    module: str,
    version: str,
    count: int,
    per_call: int,
    output_path: Path | None,
    seed: int | None,
    dry_run: bool,
) -> int:
    setup = _setup_run(
        module=module, version=version, count=count, per_call=per_call,
        output_path=output_path, seed=seed, dry_run=dry_run,
    )
    if isinstance(setup, int):
        return setup
    chunks, pool, next_qid, next_aid, output_path, tier, model = setup

    import anthropic
    client = anthropic.Anthropic(api_key=_anthropic_key())

    stats = {"n_calls": 0, "n_raw": 0, "n_valid": 0, "n_invalid": 0, "errors": []}
    written = 0
    with open(output_path, "w", encoding="utf-8") as fout:
        for i, chunk in enumerate(chunks, 1):
            if written >= count:
                break
            fewshot = pick_few_shot(pool, k=3, seed=(seed or 0) + i)
            fewshot_text = format_few_shot(fewshot)
            user_prompt = _build_user_prompt(
                chunk=chunk, module=module, version=version,
                per_call=per_call, few_shot_text=fewshot_text,
            )
            t0 = time.time()
            try:
                raw_text = call_claude(client, system=SYSTEM_PROMPT, user=user_prompt, model=model)
                arr = _parse_json_array(raw_text)
            except Exception as e:
                stats["errors"].append(f"chunk {chunk['chunk_id']}: {type(e).__name__}: {e}")
                print(f"[{i}/{len(chunks)}] ❌ {chunk['chunk_id']} → {type(e).__name__}: {e}",
                      flush=True)
                continue
            stats["n_calls"] += 1
            stats["n_raw"] += len(arr)

            valid_qs, next_qid, next_aid, n_inv, errs = _process_raw_questions(
                arr=arr, chunk=chunk, module=module, tier=tier, version=version,
                next_qid=next_qid, next_aid=next_aid, count_remaining=count - written,
            )
            for q in valid_qs:
                fout.write(json.dumps(q, ensure_ascii=False) + "\n")
                fout.flush()
                stats["n_valid"] += 1
                written += 1
            stats["n_invalid"] += n_inv
            stats["errors"].extend(errs)

            elapsed = time.time() - t0
            print(f"[{i}/{len(chunks)}] {chunk['chunk_id'][:50]:50}  → "
                  f"{len(valid_qs)}/{len(arr)} ok  ({elapsed:.1f}s)", flush=True)

    _print_summary(stats, output_path)
    return 0


async def run_generation_async(
    *,
    module: str,
    version: str,
    count: int,
    per_call: int,
    output_path: Path | None,
    seed: int | None,
    dry_run: bool,
    concurrency: int,
) -> int:
    """Variante async — appels Claude parallélisés sous semaphore."""
    setup = _setup_run(
        module=module, version=version, count=count, per_call=per_call,
        output_path=output_path, seed=seed, dry_run=dry_run,
    )
    if isinstance(setup, int):
        return setup
    chunks, pool, next_qid_init, next_aid_init, output_path, tier, model = setup

    from anthropic import AsyncAnthropic
    aclient = AsyncAnthropic(api_key=_anthropic_key())

    sem = asyncio.Semaphore(concurrency)
    print(f"→ Mode async, concurrency={concurrency}\n")

    async def one_call(idx: int, chunk: dict) -> tuple[int, list[dict] | None, float, str | None]:
        """Retourne (idx, raw_array_or_None, elapsed_s, error_msg_or_None)."""
        fewshot = pick_few_shot(pool, k=3, seed=(seed or 0) + idx)
        fewshot_text = format_few_shot(fewshot)
        user_prompt = _build_user_prompt(
            chunk=chunk, module=module, version=version,
            per_call=per_call, few_shot_text=fewshot_text,
        )
        async with sem:
            t0 = time.time()
            try:
                raw_text = await call_claude_async(
                    aclient, system=SYSTEM_PROMPT, user=user_prompt, model=model,
                )
                arr = _parse_json_array(raw_text)
                elapsed = time.time() - t0
                return idx, arr, elapsed, None
            except Exception as e:
                elapsed = time.time() - t0
                return idx, None, elapsed, f"{type(e).__name__}: {e}"

    tasks = [asyncio.create_task(one_call(i, c)) for i, c in enumerate(chunks, 1)]

    stats = {"n_calls": 0, "n_raw": 0, "n_valid": 0, "n_invalid": 0, "errors": []}
    next_qid = next_qid_init
    next_aid = next_aid_init
    written = 0
    t_start = time.time()

    # On itère sur les tasks dans l'ordre de complétion pour afficher la progression.
    # Mais on traite et écrit en série pour préserver l'ordre des IDs.
    results: dict[int, tuple[list[dict] | None, float, str | None]] = {}
    completed_done = 0
    for fut in asyncio.as_completed(tasks):
        idx, arr, elapsed, err = await fut
        results[idx] = (arr, elapsed, err)
        completed_done += 1
        chunk_short = chunks[idx - 1]["chunk_id"][:50]
        if err:
            print(f"[{completed_done}/{len(chunks)}] ❌ {chunk_short} → {err} ({elapsed:.1f}s)",
                  flush=True)
        else:
            print(f"[{completed_done}/{len(chunks)}] ✓ {chunk_short} → {len(arr)} q brutes ({elapsed:.1f}s)",
                  flush=True)

    # Maintenant on traite dans l'ordre original (idx croissant) pour garder
    # des IDs séquentiels et des fichiers déterministes.
    with open(output_path, "w", encoding="utf-8") as fout:
        for idx in sorted(results.keys()):
            if written >= count:
                break
            arr, elapsed, err = results[idx]
            if err is not None:
                stats["errors"].append(f"chunk {chunks[idx-1]['chunk_id']}: {err}")
                continue
            stats["n_calls"] += 1
            stats["n_raw"] += len(arr)
            valid_qs, next_qid, next_aid, n_inv, errs = _process_raw_questions(
                arr=arr, chunk=chunks[idx - 1], module=module, tier=tier, version=version,
                next_qid=next_qid, next_aid=next_aid, count_remaining=count - written,
            )
            for q in valid_qs:
                fout.write(json.dumps(q, ensure_ascii=False) + "\n")
                stats["n_valid"] += 1
                written += 1
            stats["n_invalid"] += n_inv
            stats["errors"].extend(errs)
        fout.flush()

    total_elapsed = time.time() - t_start
    print(f"\n(temps total async : {total_elapsed:.1f}s)")
    _print_summary(stats, output_path)
    return 0


def _print_summary(stats: dict, output_path: Path) -> None:
    print()
    print("=== Résumé ===")
    print(f"  Appels Claude    : {stats['n_calls']}")
    print(f"  Questions brutes : {stats['n_raw']}")
    print(f"  Valides écrites  : {stats['n_valid']}")
    print(f"  Invalides        : {stats['n_invalid']}")
    if stats["errors"]:
        print(f"\n  Erreurs ({len(stats['errors'])}, 10 premières) :")
        for err in stats["errors"][:10]:
            print(f"    - {err}")
    print(f"\n→ Sortie : {output_path}")


# --- Main --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Génération de questions QCM via Claude")
    parser.add_argument("--module", default=DEFAULT_MODULE)
    parser.add_argument("--version", default=DEFAULT_VERSION, choices=["18.0", "19.0"])
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT,
                        help=f"Nombre total de questions à générer (défaut : {DEFAULT_COUNT})")
    parser.add_argument("--per-call", type=int, default=DEFAULT_PER_CALL,
                        help=f"Questions par appel Claude (défaut : {DEFAULT_PER_CALL})")
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed pour la sélection chunks/few-shot (reproductibilité)")
    parser.add_argument("--output", type=Path, default=None,
                        help="Fichier JSONL de sortie (défaut : data/generated_pending/<auto>.jsonl)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Liste les chunks sélectionnés sans appeler Claude")
    parser.add_argument("--async", action="store_true", dest="async_mode",
                        help="Mode asynchrone — appels Claude parallélisés sous semaphore")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Nombre d'appels Claude simultanés en mode async (défaut : {DEFAULT_CONCURRENCY})")
    args = parser.parse_args()

    common_kwargs = dict(
        module=args.module,
        version=args.version,
        count=args.count,
        per_call=args.per_call,
        output_path=args.output,
        seed=args.seed,
        dry_run=args.dry_run,
    )
    if args.async_mode:
        return asyncio.run(run_generation_async(**common_kwargs, concurrency=args.concurrency))
    return run_generation(**common_kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
