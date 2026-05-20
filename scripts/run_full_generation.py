#!/usr/bin/env python3
"""Phase 5.5.a — Orchestrateur multi-modules de génération de questions QCM.

Lit `data/generation_plan.json` (produit par `plan_generation.py`) et soumet
**un seul gros batch Anthropic** couvrant toutes les paires (tier, module,
version) avec target_total > 0. Les résultats sont parsés et écrits par
(module, version) dans `data/generated_pending/<module>-v<ver>-<batch_id>.jsonl`.

Workflow :

    # 1) Aperçu — pas d'appel LLM, juste plan + estim coût + durée
    python3 -m scripts.run_full_generation --dry-run

    # 2) Submit le batch (retourne batch_id, exit immédiat)
    python3 -m scripts.run_full_generation --submit

    # 3) Poll un batch en cours (bloque jusqu'à ended, puis parse)
    python3 -m scripts.run_full_generation --poll <batch_id>

    # 4) Tout en une commande (submit + poll + parse — bloquant)
    python3 -m scripts.run_full_generation --run

State file : data/run_state.json — track batch_id + mapping custom_id →
(module, version, chunk_id). Permet le `--poll <batch_id>` et la reprise
en cas d'interruption réseau.

Voir SESSION-NOTES.md pour la spec détaillée.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm import _anthropic_key, _answer_model  # noqa: E402
from app.odoo_docs_rag import db_path  # noqa: E402
from app.study_modules import tier_of  # noqa: E402

from scripts.generate_questions import (  # noqa: E402
    DEFAULT_PER_CALL,
    PENDING_DIR,
    QUESTIONS_FILE,
    SYSTEM_PROMPT,
    _build_user_prompt,
    _max_answer_id,
    _next_question_id,
    _parse_json_array,
    _process_raw_questions,
    _system_blocks,
    format_few_shot,
    load_few_shot_pool,
    load_udemy_modules_map,
    pick_few_shot,
    select_chunks,
)

PLAN_FILE = ROOT / "data" / "generation_plan.json"
STATE_FILE = ROOT / "data" / "run_state.json"

# Tarifs Anthropic Batch API Sonnet 4.6 (cf. docs.anthropic.com)
# Pricing par MTok (1e6 tokens) — Batch = -50 % du tarif standard
PRICE_INPUT_BASE_MT = 1.50   # input non-cached (50 % de $3)
PRICE_INPUT_CACHED_MT = 0.30  # input cached (50 % de $0.60)
PRICE_OUTPUT_MT = 7.50       # output (50 % de $15)

# Estimations conservatives (mesurées sur la mini-run 5.4)
EST_INPUT_TOKENS_FIRST = 1800     # 1er appel d'un batch : system complet
EST_INPUT_TOKENS_CACHED = 200     # appels suivants : cache hit sur system
EST_OUTPUT_TOKENS_PER_CALL = 1800  # ~4 questions × ~450 tokens chacune

DEFAULT_SEED = 42


# --- État d'un run ----------------------------------------------------------


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --- Construction des requêtes Batch ---------------------------------------


def _flatten_plan(plan: dict) -> list[dict]:
    """Aplatit le plan en liste d'entrées (module, version, target_total, tier).

    Skip les (module, version) avec target_total == 0 pour économiser des appels.
    """
    out: list[dict] = []
    for tier_name, tier_block in plan.get("tiers", {}).items():
        for mod in tier_block.get("modules", []):
            for ver_key, target_key in (("18.0", "target_v18"), ("19.0", "target_v19")):
                target = int(mod.get(target_key) or 0)
                if target <= 0:
                    continue
                out.append({
                    "tier": tier_name,
                    "module": mod["module"],
                    "version": ver_key,
                    "target": target,
                })
    return out


def _custom_id(entry: dict, chunk_idx: int) -> str:
    """ID stable identifiant une requête dans le batch."""
    mod_slug = entry["module"].replace("/", "__")
    return f"{entry['tier']}/{mod_slug}/v{entry['version']}/c{chunk_idx:03d}"


def build_batch_requests(
    plan: dict,
    *,
    per_call: int,
    seed: int,
    model: str,
    pool: list[dict],
    modules_map: dict[int, str],
) -> tuple[list[dict], dict[str, dict]]:
    """Construit la liste des requêtes pour le Batch et le mapping custom_id → meta.

    Retourne :
      requests  : list[Request] (format Anthropic Batch)
      mapping   : { custom_id: { module, version, tier, chunk: {chunk_id, url, ...}, per_call } }
    """
    entries = _flatten_plan(plan)
    if not entries:
        return [], {}

    conn = sqlite3.connect(db_path())
    requests: list[dict] = []
    mapping: dict[str, dict] = {}
    try:
        for entry in entries:
            n_calls = math.ceil(entry["target"] / per_call)
            chunks = select_chunks(
                conn,
                version=entry["version"],
                module=entry["module"],
                n=n_calls,
                seed=seed,
            )
            if not chunks:
                print(f"⚠️  pas de chunk pour {entry['module']} v{entry['version']}",
                      file=sys.stderr)
                continue

            for chunk_idx, chunk in enumerate(chunks):
                fewshot = pick_few_shot(
                    pool, k=3,
                    seed=seed + chunk_idx + hash(entry["module"]) % 100_000,
                    module=entry["module"], modules_map=modules_map,
                )
                fewshot_text = format_few_shot(fewshot)
                user_prompt = _build_user_prompt(
                    chunk=chunk, module=entry["module"], version=entry["version"],
                    per_call=per_call, few_shot_text=fewshot_text,
                )
                cid = _custom_id(entry, chunk_idx)
                params = {
                    "model": model,
                    "max_tokens": 4096,
                    "system": _system_blocks(SYSTEM_PROMPT),
                    "messages": [{"role": "user", "content": user_prompt}],
                }
                requests.append({"custom_id": cid, "params": params})
                mapping[cid] = {
                    "tier": entry["tier"],
                    "module": entry["module"],
                    "version": entry["version"],
                    "chunk": {
                        "chunk_id": chunk["chunk_id"],
                        "url": chunk["url"],
                        "title": chunk["title"],
                        "section": chunk["section"],
                        "text": chunk["text"],
                    },
                    "per_call": per_call,
                }
    finally:
        conn.close()
    return requests, mapping


# --- Coût et durée estimés -------------------------------------------------


def estimate_cost_and_time(n_requests: int) -> dict:
    """Calcule estimation conservatrice coût Batch + durée file d'attente."""
    # Caching : 1 cache miss au début, le reste en cache hit
    n_first = 1
    n_cached = max(0, n_requests - 1)

    tokens_in_first = n_first * EST_INPUT_TOKENS_FIRST
    tokens_in_cached = n_cached * EST_INPUT_TOKENS_CACHED
    tokens_out = n_requests * EST_OUTPUT_TOKENS_PER_CALL

    cost_in_first = tokens_in_first * PRICE_INPUT_BASE_MT / 1e6
    cost_in_cached = tokens_in_cached * PRICE_INPUT_CACHED_MT / 1e6
    cost_out = tokens_out * PRICE_OUTPUT_MT / 1e6
    total = cost_in_first + cost_in_cached + cost_out

    # Anthropic Batch : SLA 24h, en pratique 10-60 min pour batches modérés
    eta_min_low = max(5, n_requests // 20)
    eta_min_high = max(30, n_requests // 10)

    return {
        "n_requests": n_requests,
        "tokens_input_first": tokens_in_first,
        "tokens_input_cached": tokens_in_cached,
        "tokens_output": tokens_out,
        "cost_input_first_usd": round(cost_in_first, 4),
        "cost_input_cached_usd": round(cost_in_cached, 4),
        "cost_output_usd": round(cost_out, 4),
        "cost_total_usd": round(total, 2),
        "eta_minutes_low": eta_min_low,
        "eta_minutes_high": eta_min_high,
    }


def print_plan_summary(plan: dict, requests: list[dict], est: dict) -> None:
    entries = _flatten_plan(plan)
    print()
    print("=" * 70)
    print("PLAN DE GÉNÉRATION — Phase 5.5.a")
    print("=" * 70)
    print(f"Cible totale          : {plan['meta']['grand_total']} questions")
    print(f"Overflow              : {plan['meta'].get('total_overflow', 0)}")
    print(f"Paires (module,vers.) : {len(entries)}")
    print(f"Appels Claude (Batch) : {len(requests)} (per_call="
          f"{requests[0]['params'].get('_per_call', '?') if requests else '?'})")
    print()

    # Tableau par tier
    from collections import defaultdict
    per_tier = defaultdict(lambda: {"calls": 0, "target": 0})
    for r in requests:
        cid = r["custom_id"]
        tier = cid.split("/", 1)[0]
        per_tier[tier]["calls"] += 1
    for e in entries:
        per_tier[e["tier"]]["target"] += e["target"]

    print(f"{'Tier':<8} {'Target Q':>10} {'Calls':>8}")
    print(f"{'-'*8} {'-'*10} {'-'*8}")
    for tier_name in sorted(per_tier.keys()):
        s = per_tier[tier_name]
        print(f"{tier_name:<8} {s['target']:>10} {s['calls']:>8}")
    print()

    print("--- ESTIMATION COÛT (Batch API, prompt caching actif) ---")
    print(f"Tokens input  (1er, non-cached) : {est['tokens_input_first']:>10,}  "
          f"→ ${est['cost_input_first_usd']:>7.4f}")
    print(f"Tokens input  (cached @ -90%)   : {est['tokens_input_cached']:>10,}  "
          f"→ ${est['cost_input_cached_usd']:>7.4f}")
    print(f"Tokens output                    : {est['tokens_output']:>10,}  "
          f"→ ${est['cost_output_usd']:>7.4f}")
    print(f"{'TOTAL':<32}              ${est['cost_total_usd']:>7.2f}")
    print()
    print(f"--- ETA Batch Anthropic ---")
    print(f"Fourchette : {est['eta_minutes_low']}–{est['eta_minutes_high']} minutes "
          f"(SLA 24h, pratique 10-60 min)")
    print()


# --- Submit / Poll / Parse --------------------------------------------------


def submit_batch(client, requests: list[dict]) -> str:
    """Soumet le batch à Anthropic. Retourne le batch_id."""
    from anthropic.types.messages.batch_create_params import Request

    # SDK >= 0.34 : client.messages.batches.create(requests=[Request(...)])
    raw_requests = [
        Request(custom_id=r["custom_id"], params=r["params"]) for r in requests
    ]
    batch = client.messages.batches.create(requests=raw_requests)
    return batch.id


def poll_batch(client, batch_id: str, *, interval_s: int = 30, max_wait_s: int = 3600 * 6) -> dict:
    """Bloque jusqu'à ce que le batch soit `ended`. Retourne l'objet batch final."""
    t0 = time.time()
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        status = getattr(batch, "processing_status", None) or getattr(batch, "status", None)
        counts = getattr(batch, "request_counts", None)
        elapsed = int(time.time() - t0)
        c_str = ""
        if counts is not None:
            c_str = (f" | succeeded={getattr(counts, 'succeeded', 0)} "
                     f"errored={getattr(counts, 'errored', 0)} "
                     f"processing={getattr(counts, 'processing', 0)}")
        print(f"[{elapsed//60}m{elapsed%60:02d}s] batch={batch_id[:20]} status={status}{c_str}",
              flush=True)
        if status == "ended":
            return batch
        if status in ("canceled", "expired", "failed"):
            raise RuntimeError(f"Batch terminé en erreur — status={status}")
        if time.time() - t0 > max_wait_s:
            raise TimeoutError(f"Batch toujours {status} après {max_wait_s}s")
        time.sleep(interval_s)


def download_results(client, batch_id: str) -> list[dict]:
    """Streame les résultats du batch, retourne list[dict] avec custom_id + result."""
    out: list[dict] = []
    # SDK retourne un itérateur de MessageBatchIndividualResponse
    for r in client.messages.batches.results(batch_id):
        # r.custom_id, r.result.{type, message?, error?}
        result_obj = r.result
        rtype = getattr(result_obj, "type", "?")
        record = {"custom_id": r.custom_id, "type": rtype}
        if rtype == "succeeded":
            msg = result_obj.message
            parts = []
            for blk in msg.content or []:
                if getattr(blk, "type", None) == "text":
                    parts.append(getattr(blk, "text", "") or "")
            record["text"] = "".join(parts).strip()
            usage = getattr(msg, "usage", None)
            if usage is not None:
                record["usage"] = {
                    "input_tokens": getattr(usage, "input_tokens", 0),
                    "output_tokens": getattr(usage, "output_tokens", 0),
                    "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0),
                }
        else:
            record["error"] = str(getattr(result_obj, "error", result_obj))
        out.append(record)
    return out


def parse_results_to_pending(
    results: list[dict],
    mapping: dict[str, dict],
    *,
    batch_id: str,
) -> dict:
    """Parse les résultats + assemble les questions + écrit les pending JSONL
    par (module, version). Retourne stats agrégées.
    """
    PENDING_DIR.mkdir(parents=True, exist_ok=True)

    # Charger banque pour next_qid / next_aid (réservation séquentielle)
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        bank_data = json.load(f)
    bank_qs = bank_data.get("questions") or []
    next_qid = _next_question_id(bank_qs)
    next_aid = _max_answer_id(bank_qs) + 1

    # Regrouper par (module, version) pour grouper les écritures
    from collections import defaultdict
    by_target: dict[tuple[str, str], list[dict]] = defaultdict(list)

    stats = {
        "total_results": len(results),
        "succeeded": 0,
        "errored": 0,
        "no_mapping": 0,
        "parse_failed": 0,
        "n_raw": 0,
        "n_valid": 0,
        "n_invalid": 0,
        "usage_input_total": 0,
        "usage_output_total": 0,
        "usage_cache_read_total": 0,
    }

    for r in results:
        cid = r["custom_id"]
        meta = mapping.get(cid)
        if meta is None:
            stats["no_mapping"] += 1
            continue
        if r["type"] != "succeeded":
            stats["errored"] += 1
            continue
        stats["succeeded"] += 1

        usage = r.get("usage") or {}
        stats["usage_input_total"] += int(usage.get("input_tokens", 0))
        stats["usage_output_total"] += int(usage.get("output_tokens", 0))
        stats["usage_cache_read_total"] += int(usage.get("cache_read_input_tokens", 0))

        text = r.get("text") or ""
        try:
            arr = _parse_json_array(text)
        except Exception as e:
            stats["parse_failed"] += 1
            print(f"  parse fail {cid}: {e}", file=sys.stderr)
            continue
        stats["n_raw"] += len(arr)

        valid_qs, next_qid, next_aid, n_inv, _errs = _process_raw_questions(
            arr=arr, chunk=meta["chunk"],
            module=meta["module"], tier=meta["tier"], version=meta["version"],
            next_qid=next_qid, next_aid=next_aid,
            count_remaining=meta["per_call"] * 2,  # latitude : on prend tout ce qui est valide
        )
        stats["n_invalid"] += n_inv
        stats["n_valid"] += len(valid_qs)
        by_target[(meta["module"], meta["version"])].extend(valid_qs)

    # Écriture des JSONL par (module, version)
    written_files: list[str] = []
    for (module, version), qs in by_target.items():
        if not qs:
            continue
        slug = module.replace("/", "__")
        path = PENDING_DIR / f"{slug}-v{version}-{batch_id}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for q in qs:
                f.write(json.dumps(q, ensure_ascii=False) + "\n")
        written_files.append(str(path.relative_to(ROOT)))

    stats["pending_files"] = written_files
    stats["next_qid_after_batch"] = next_qid
    return stats


# --- Modes principaux ------------------------------------------------------


def cmd_dry_run(plan_path: Path, seed: int, per_call: int) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    pool = load_few_shot_pool()
    modules_map = load_udemy_modules_map()
    model = _answer_model()

    requests, mapping = build_batch_requests(
        plan, per_call=per_call, seed=seed, model=model,
        pool=pool, modules_map=modules_map,
    )
    est = estimate_cost_and_time(len(requests))

    # Hack pour print_plan_summary : on injecte _per_call dans le 1er params
    if requests:
        requests[0]["params"]["_per_call"] = per_call
    print_plan_summary(plan, requests, est)

    print(f"Modèle  : {model}")
    print(f"Plan    : {plan_path}")
    print(f"State   : {STATE_FILE} (non écrit en dry-run)")
    print()
    print("Pour soumettre le batch : --submit  (puis --poll <batch_id>)")
    print("Pour tout enchaîner     : --run")
    return 0


def cmd_submit(plan_path: Path, seed: int, per_call: int) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    pool = load_few_shot_pool()
    modules_map = load_udemy_modules_map()
    model = _answer_model()

    requests, mapping = build_batch_requests(
        plan, per_call=per_call, seed=seed, model=model,
        pool=pool, modules_map=modules_map,
    )
    if not requests:
        print("❌ Aucune requête à soumettre.", file=sys.stderr)
        return 1

    import anthropic
    client = anthropic.Anthropic(api_key=_anthropic_key())
    print(f"→ Soumission batch ({len(requests)} requêtes)…")
    batch_id = submit_batch(client, requests)
    print(f"✓ Batch soumis : {batch_id}")

    # Sauve state
    state = _load_state()
    state[batch_id] = {
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_requests": len(requests),
        "per_call": per_call,
        "seed": seed,
        "plan_file": str(plan_path.relative_to(ROOT)),
        "model": model,
        "mapping": mapping,
        "status": "submitted",
    }
    _save_state(state)
    print(f"→ State écrit : {STATE_FILE}")
    print()
    print(f"Pour poller : python3 -m scripts.run_full_generation --poll {batch_id}")
    return 0


def cmd_poll(batch_id: str, *, interval_s: int = 30) -> int:
    state = _load_state()
    entry = state.get(batch_id)
    if entry is None:
        print(f"❌ batch_id {batch_id} absent de {STATE_FILE}", file=sys.stderr)
        return 1
    mapping = entry["mapping"]

    import anthropic
    client = anthropic.Anthropic(api_key=_anthropic_key())
    print(f"→ Poll batch {batch_id} (interval {interval_s}s)…")
    poll_batch(client, batch_id, interval_s=interval_s)
    print(f"✓ Batch terminé. Download résultats…")
    results = download_results(client, batch_id)
    print(f"✓ {len(results)} résultats récupérés. Parsing…")
    stats = parse_results_to_pending(results, mapping, batch_id=batch_id)

    entry["status"] = "parsed"
    entry["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["stats"] = stats
    _save_state(state)

    print()
    print("=" * 70)
    print("RÉSULTATS BATCH")
    print("=" * 70)
    print(f"Requests soumises     : {entry['n_requests']}")
    print(f"Réponses téléchargées : {stats['total_results']}")
    print(f"Succès                : {stats['succeeded']}")
    print(f"Erreurs API           : {stats['errored']}")
    print(f"Parse fail            : {stats['parse_failed']}")
    print(f"Questions brutes      : {stats['n_raw']}")
    print(f"Questions valides     : {stats['n_valid']}")
    print(f"Invalides écartées    : {stats['n_invalid']}")
    print()
    print(f"Tokens IN (non-cache) : {stats['usage_input_total']:,}")
    print(f"Tokens IN (cache hit) : {stats['usage_cache_read_total']:,}")
    print(f"Tokens OUT            : {stats['usage_output_total']:,}")
    cost = (
        stats['usage_input_total'] * PRICE_INPUT_BASE_MT / 1e6
        + stats['usage_cache_read_total'] * PRICE_INPUT_CACHED_MT / 1e6
        + stats['usage_output_total'] * PRICE_OUTPUT_MT / 1e6
    )
    print(f"Coût réel estimé      : ${cost:.2f}")
    print()
    print(f"Fichiers pending écrits ({len(stats['pending_files'])}) :")
    for f in stats["pending_files"][:10]:
        print(f"  - {f}")
    if len(stats["pending_files"]) > 10:
        print(f"  ... +{len(stats['pending_files']) - 10}")
    return 0


def cmd_run(plan_path: Path, seed: int, per_call: int, interval_s: int) -> int:
    """Submit + Poll + Parse, en bloquant."""
    rc = cmd_submit(plan_path, seed, per_call)
    if rc != 0:
        return rc
    # Récupérer le batch_id du dernier submit
    state = _load_state()
    latest = max(state.items(),
                 key=lambda kv: kv[1].get("submitted_at", ""))
    batch_id = latest[0]
    return cmd_poll(batch_id, interval_s=interval_s)


# --- Main ------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", type=Path, default=PLAN_FILE,
                        help=f"Plan JSON (défaut : {PLAN_FILE.relative_to(ROOT)})")
    parser.add_argument("--per-call", type=int, default=DEFAULT_PER_CALL,
                        help=f"Questions par appel Claude (défaut : {DEFAULT_PER_CALL})")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"Seed reproductibilité (défaut : {DEFAULT_SEED})")
    parser.add_argument("--poll-interval", type=int, default=30,
                        help="Intervalle de poll en secondes (défaut : 30)")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true",
                       help="Affiche plan + estim coût/durée, aucun appel LLM")
    group.add_argument("--submit", action="store_true",
                       help="Soumet le batch et exit (récupère le batch_id)")
    group.add_argument("--poll", metavar="BATCH_ID",
                       help="Poll un batch existant et parse les résultats")
    group.add_argument("--run", action="store_true",
                       help="Submit + Poll + Parse en bloquant")

    args = parser.parse_args()

    if not args.plan.exists() and not args.poll:
        print(f"❌ Plan introuvable : {args.plan}", file=sys.stderr)
        print(f"   Génère-le via : python3 -m scripts.plan_generation", file=sys.stderr)
        return 1

    if args.dry_run:
        return cmd_dry_run(args.plan, seed=args.seed, per_call=args.per_call)
    if args.submit:
        return cmd_submit(args.plan, seed=args.seed, per_call=args.per_call)
    if args.poll:
        return cmd_poll(args.poll, interval_s=args.poll_interval)
    if args.run:
        return cmd_run(args.plan, seed=args.seed, per_call=args.per_call,
                       interval_s=args.poll_interval)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
