#!/usr/bin/env python3
"""Phase 6 — Traduction FR des questions Udemy via Batch API Anthropic.

Pour chaque question Udemy (correct_answer_source == "udemy") qui a `title_fr`
ou un ou plusieurs `value_fr` vides, soumet un appel Claude Sonnet 4.6 qui
demande la traduction FR du titre EN + de toutes les options EN.

Réutilise le même PROMPT que `app.llm.translate_item_fr` (cf. briefing :
"NE PAS créer une nouvelle fonction de traduction") mais adapté Batch API.

Workflow :
    python3 -m scripts.translate_udemy_batch --dry-run     # plan + coût
    python3 -m scripts.translate_udemy_batch --submit      # submit batch
    python3 -m scripts.translate_udemy_batch --poll <id>   # poll + apply
    python3 -m scripts.translate_udemy_batch --run         # tout en un

Update atomique de questions.json (.tmp + replace) + invalidate
embedding cache après save (idempotent : ré-exécuter ne refait que les
questions encore incomplètes).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm import _anthropic_key, _answer_model  # noqa: E402

from scripts.run_full_generation import (  # noqa: E402
    PRICE_INPUT_BASE_MT,
    PRICE_INPUT_CACHED_MT,
    PRICE_OUTPUT_MT,
    download_results,
    poll_batch,
    submit_batch,
)

QUESTIONS_FILE = ROOT / "questions.json"
TRANSLATE_STATE = ROOT / "data" / "translate_state.json"

# Estimations (mesurées sur translate_item_fr historique)
EST_INPUT_TOKENS = 280   # prompt + question + 3-4 options
EST_OUTPUT_TOKENS = 160  # JSON {title_fr, answers_fr}

SYSTEM_PROMPT = """Tu traduis des questions QCM de certification Odoo de l'anglais vers le français.

Tu produis EXCLUSIVEMENT du JSON strict, aucun texte hors JSON, aucun préambule.

Règles de traduction :
- Garde le vocabulaire métier Odoo officiel quand il existe (ex. "Quotation" → "Devis", "Sales Order" → "Bon de commande", "RFQ" → "Demande de prix").
- Garde les noms de modules et de fonctionnalités tels quels s'ils n'ont pas de traduction officielle (ex. "Point of Sale" reste "Point of Sale" ou devient "Point de Vente" selon usage).
- Conserve la nuance technique : si une option dit "Set the field to active" la traduction est "Mettre le champ à actif", pas "Activer le champ".
- Pas d'inversion de sens. Si une option est négative en EN, elle reste négative en FR.
- Garde la longueur comparable : pas de paraphrase qui rallonge artificiellement.
- Tutoie/vouvoie : utilise un style neutre/professionnel (pas de "tu", privilégie l'infinitif ou le sujet implicite).

Format de sortie strict :
{
  "title_fr": "...",
  "answers_fr": ["...", "...", "...", ...]
}

Le nombre d'entrées dans `answers_fr` doit EXACTEMENT correspondre au nombre d'options EN reçues, dans le MÊME ordre.
"""


# --- Sélection des questions à traduire ------------------------------------


def needs_translation(q: dict) -> bool:
    """True si title_fr OU au moins un value_fr est vide."""
    if not (q.get("title_fr") or "").strip():
        return True
    for a in q.get("answers") or []:
        if not (a.get("value_fr") or "").strip():
            return True
    return False


def load_udemy_to_translate() -> tuple[dict, list[dict]]:
    """Retourne (bank_data complet, liste questions Udemy à traduire)."""
    if not QUESTIONS_FILE.exists():
        raise SystemExit(f"❌ {QUESTIONS_FILE} introuvable")
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        bank = json.load(f)
    qs = bank.get("questions") or []
    todo = [
        q for q in qs
        if isinstance(q, dict)
        and q.get("correct_answer_source") == "udemy"
        and isinstance(q.get("id"), int)
        and q["id"] > 0
        and needs_translation(q)
    ]
    return bank, todo


# --- Build batch requests --------------------------------------------------


def _user_prompt(q: dict) -> str:
    options = [a.get("value") or "" for a in q.get("answers") or []]
    n = len(options)
    lines = "\n".join(f"  {i + 1}. {a}" for i, a in enumerate(options))
    return f"""Question (EN) :
{q.get('title') or ''}

Options (EN) :
{lines}

Réponds avec le JSON contenant exactement {n} entrées dans `answers_fr`."""


def build_translate_requests(todo: list[dict], model: str) -> tuple[list[dict], dict[str, int]]:
    """Retourne (requests, mapping custom_id → qid)."""
    requests: list[dict] = []
    mapping: dict[str, int] = {}
    for q in todo:
        qid = int(q["id"])
        cid = f"t_{qid:05d}"  # ex. 't_00942', dans le pattern Anthropic
        params = {
            "model": model,
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _user_prompt(q)}],
        }
        requests.append({"custom_id": cid, "params": params})
        mapping[cid] = qid
    return requests, mapping


# --- Apply results ---------------------------------------------------------


def _parse_json_object(text: str) -> dict | None:
    """Extrait un dict JSON de la réponse Claude (résiste aux balises md)."""
    import re
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None
    try:
        return json.loads(text[s:e + 1])
    except json.JSONDecodeError:
        return None


def apply_translations(
    results: list[dict], mapping: dict[str, int], bank: dict
) -> dict:
    """Update bank in-place avec les traductions et retourne stats."""
    qs_by_id = {int(q["id"]): q for q in bank.get("questions") or [] if isinstance(q, dict) and isinstance(q.get("id"), int)}

    stats = {
        "n_results": len(results),
        "n_ok": 0,
        "n_errored": 0,
        "n_parse_fail": 0,
        "n_count_mismatch": 0,
        "n_applied": 0,
        "usage_input_total": 0,
        "usage_output_total": 0,
        "usage_cache_read_total": 0,
    }

    for r in results:
        cid = r["custom_id"]
        qid = mapping.get(cid)
        if qid is None or r["type"] != "succeeded":
            stats["n_errored"] += 1
            continue
        stats["n_ok"] += 1

        usage = r.get("usage") or {}
        stats["usage_input_total"] += int(usage.get("input_tokens", 0))
        stats["usage_output_total"] += int(usage.get("output_tokens", 0))
        stats["usage_cache_read_total"] += int(usage.get("cache_read_input_tokens", 0))

        obj = _parse_json_object(r.get("text") or "")
        if obj is None:
            stats["n_parse_fail"] += 1
            continue

        title_fr = (obj.get("title_fr") or "").strip()
        answers_fr = obj.get("answers_fr") or []
        if not isinstance(answers_fr, list):
            stats["n_parse_fail"] += 1
            continue

        q = qs_by_id.get(qid)
        if q is None:
            stats["n_errored"] += 1
            continue

        if len(answers_fr) != len(q.get("answers") or []):
            stats["n_count_mismatch"] += 1
            continue

        # Update only empty fields (idempotent)
        if title_fr and not (q.get("title_fr") or "").strip():
            q["title_fr"] = title_fr
        for a, vfr in zip(q.get("answers") or [], answers_fr):
            vfr = (vfr or "").strip()
            if vfr and not (a.get("value_fr") or "").strip():
                a["value_fr"] = vfr
        stats["n_applied"] += 1

    return stats


def save_bank_atomic(bank: dict) -> None:
    """Atomic save + invalidate embedding cache (cf. briefing)."""
    tmp = QUESTIONS_FILE.with_suffix(".tmp")
    # Backup horodaté (briefing : règle de pilotage)
    backup = QUESTIONS_FILE.parent / f"questions.json.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    if QUESTIONS_FILE.exists():
        backup.write_bytes(QUESTIONS_FILE.read_bytes())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bank, f, ensure_ascii=False, indent=2)
    tmp.replace(QUESTIONS_FILE)
    try:
        from bank_embeddings import invalidate_embedding_cache  # noqa: E402
        invalidate_embedding_cache()
    except Exception as e:
        print(f"⚠️  invalidate_embedding_cache : {e}", file=sys.stderr)


# --- Estimation -------------------------------------------------------------


def estimate_cost(n_requests: int) -> dict:
    tokens_in = n_requests * EST_INPUT_TOKENS
    tokens_out = n_requests * EST_OUTPUT_TOKENS
    cost_in = tokens_in * PRICE_INPUT_BASE_MT / 1e6
    cost_out = tokens_out * PRICE_OUTPUT_MT / 1e6
    return {
        "n_requests": n_requests,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_in_usd": round(cost_in, 4),
        "cost_out_usd": round(cost_out, 4),
        "cost_total_usd": round(cost_in + cost_out, 2),
        "eta_min_low": max(5, n_requests // 20),
        "eta_min_high": max(30, n_requests // 10),
    }


# --- State ---------------------------------------------------------------


def _load_state() -> dict:
    if not TRANSLATE_STATE.exists():
        return {}
    try:
        return json.loads(TRANSLATE_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    TRANSLATE_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TRANSLATE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TRANSLATE_STATE)


# --- Commands -------------------------------------------------------------


def cmd_dry_run() -> int:
    bank, todo = load_udemy_to_translate()
    n_total_udemy = sum(
        1 for q in bank.get("questions") or []
        if isinstance(q, dict) and q.get("correct_answer_source") == "udemy"
        and isinstance(q.get("id"), int) and q["id"] > 0
    )
    est = estimate_cost(len(todo))
    print()
    print("=" * 70)
    print("PHASE 6 — TRADUCTION FR UDEMY — DRY RUN")
    print("=" * 70)
    print(f"Total questions Udemy : {n_total_udemy}")
    print(f"Déjà traduites (skip) : {n_total_udemy - len(todo)}")
    print(f"À traduire            : {len(todo)}")
    print()
    print("--- COÛT ESTIMÉ ---")
    print(f"Tokens IN  : {est['tokens_in']:>10,}  → ${est['cost_in_usd']:.4f}")
    print(f"Tokens OUT : {est['tokens_out']:>10,}  → ${est['cost_out_usd']:.4f}")
    print(f"TOTAL      : ${est['cost_total_usd']:.2f}")
    print(f"ETA        : {est['eta_min_low']}-{est['eta_min_high']} min")
    print()
    print(f"Modèle     : {_answer_model()}")
    print(f"Banque     : {QUESTIONS_FILE}")
    return 0


def cmd_submit() -> int:
    bank, todo = load_udemy_to_translate()
    if not todo:
        print("Aucune question à traduire.")
        return 0
    model = _answer_model()
    requests, mapping = build_translate_requests(todo, model)

    import anthropic
    client = anthropic.Anthropic(api_key=_anthropic_key())
    print(f"→ Submit batch trad FR ({len(requests)} requêtes)…")
    batch_id = submit_batch(client, requests)
    print(f"✓ Batch trad soumis : {batch_id}")

    state = _load_state()
    state[batch_id] = {
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_requests": len(requests),
        "model": model,
        "mapping": mapping,
        "status": "submitted",
    }
    _save_state(state)
    print(f"Pour poller : python3 -m scripts.translate_udemy_batch --poll {batch_id}")
    return 0


def cmd_poll(batch_id: str, *, interval_s: int = 60) -> int:
    state = _load_state()
    entry = state.get(batch_id)
    if entry is None:
        print(f"❌ batch_id {batch_id} absent de {TRANSLATE_STATE}", file=sys.stderr)
        return 1
    mapping = entry["mapping"]

    import anthropic
    client = anthropic.Anthropic(api_key=_anthropic_key())
    print(f"→ Poll batch trad {batch_id}…")
    poll_batch(client, batch_id, interval_s=interval_s)
    print("✓ Batch terminé. Download résultats…")
    results = download_results(client, batch_id)
    print(f"✓ {len(results)} résultats. Application…")

    # Re-read banque au moment de l'apply (le full run peut l'avoir modifiée)
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        bank = json.load(f)
    stats = apply_translations(results, mapping, bank)

    save_bank_atomic(bank)

    entry["status"] = "applied"
    entry["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["stats"] = stats
    _save_state(state)

    print()
    print("=" * 70)
    print("TRADUCTIONS APPLIQUÉES")
    print("=" * 70)
    print(f"Requêtes        : {entry['n_requests']}")
    print(f"OK / errored    : {stats['n_ok']} / {stats['n_errored']}")
    print(f"Parse fail      : {stats['n_parse_fail']}")
    print(f"Count mismatch  : {stats['n_count_mismatch']}")
    print(f"Appliquées      : {stats['n_applied']}")
    print(f"Tokens IN       : {stats['usage_input_total']:,}")
    print(f"Tokens OUT      : {stats['usage_output_total']:,}")
    cost = (
        stats['usage_input_total'] * PRICE_INPUT_BASE_MT / 1e6
        + stats['usage_output_total'] * PRICE_OUTPUT_MT / 1e6
    )
    print(f"Coût réel       : ${cost:.2f}")
    print()
    print(f"questions.json sauvegardé (atomic) + cache embeddings invalidé.")
    return 0


def cmd_run(interval_s: int) -> int:
    rc = cmd_submit()
    if rc != 0:
        return rc
    state = _load_state()
    if not state:
        return 1
    latest = max(state.items(), key=lambda kv: kv[1].get("submitted_at", ""))
    return cmd_poll(latest[0], interval_s=interval_s)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--poll-interval", type=int, default=60)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--submit", action="store_true")
    g.add_argument("--poll", metavar="BATCH_ID")
    g.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        return cmd_dry_run()
    if args.submit:
        return cmd_submit()
    if args.poll:
        return cmd_poll(args.poll, interval_s=args.poll_interval)
    if args.run:
        return cmd_run(args.poll_interval)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
