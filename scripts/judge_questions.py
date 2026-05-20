#!/usr/bin/env python3
"""Phase 5.5.b — Pipeline judge des questions pending.

Pour chaque question pending (issue de l'orchestrateur 5.5.a), demande à Claude
Sonnet 4.6 de noter sur 5 critères (1-5 chacun). Le score final est le MIN des
5 critères (maillon faible). Decision :

    score >= 4  → accept   → status: "verified_by_judge"
    score == 3  → review   → status: "unverified"
    score <= 2  → reject   → status: "flagged" + log dans rejected_questions.jsonl

Optim coût : les questions sont GROUPÉES par `source_chunk_id` (≤ 4 questions
issues du même chunk = même source de vérité) → 1 appel judge par groupe au
lieu d'1 par question. Ça divise par ~4 le nombre d'appels et amortit le
contexte du chunk via prompt caching.

Pipeline Batch API d'Anthropic, mono-batch sur tous les fichiers pending.

Usage :
    python3 -m scripts.judge_questions --dry-run                # plan + coût
    python3 -m scripts.judge_questions --submit                 # soumet le batch
    python3 -m scripts.judge_questions --poll <batch_id>        # poll + parse
    python3 -m scripts.judge_questions --run                    # tout enchaîner

Fichiers touchés :
    data/generated_pending/*.jsonl  — updated in-place (champs judge_*)
    data/rejected_questions.jsonl   — append-only, questions rejetées
    data/judge_state.json           — track batch_id + mapping
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

PENDING_DIR = ROOT / "data" / "generated_pending"
REJECTED_LOG = ROOT / "data" / "rejected_questions.jsonl"
JUDGE_STATE = ROOT / "data" / "judge_state.json"

# Estimations conservatives
EST_INPUT_TOKENS_FIRST = 1500   # system judge + chunk + 4 q
EST_INPUT_TOKENS_CACHED = 600   # cache hit sur system judge ; chunk varie
EST_OUTPUT_TOKENS_PER_CALL = 700  # 4 scores JSON × ~150 tokens

JUDGE_SYSTEM = """Tu es un juge expert pour des questions QCM de certification fonctionnelle Odoo (versions 18/19).

Ton rôle : noter la qualité d'un lot de 1 à 4 questions issues d'un MÊME chunk de doc Odoo officielle. Tu reçois le chunk source ET les questions générées. Tu vérifies que chaque question est correcte ET de qualité pour un examen de certification.

Tu produis EXCLUSIVEMENT du JSON strict, aucun texte hors JSON, aucun préambule.

# 5 critères de notation (1 = mauvais, 5 = excellent)

1. **factualite** : la bonne réponse est-elle factuellement correcte et confirmée par le chunk fourni ? Les distracteurs sont-ils bien INCORRECTS (pas des cas où plusieurs réponses pourraient être justes) ?
   - 5 : bonne réponse littéralement dans le chunk, distracteurs indiscutablement faux
   - 3 : bonne réponse plausible mais inférée plutôt que directement attestée
   - 1 : bonne réponse fausse OU plusieurs options sont vraies OU contredit le chunk

2. **clarte** : le titre et les options sont-ils non-ambigus, sans double négation, sans formulation alambiquée ?
   - 5 : titre net, options sans ambiguïté, aucun jargon non défini
   - 3 : compréhensible mais lecture en 2 temps
   - 1 : ambigu, mal formulé, ou interprétable de plusieurs façons

3. **distracteurs** : les distracteurs sont-ils plausibles (concepts proches, pas absurdes), de longueur comparable à la bonne réponse, et non triviaux à éliminer ?
   - 5 : 3 distracteurs solides, longueurs équilibrées (±5 mots)
   - 3 : 1 distracteur trop évident OU longueurs inégales
   - 1 : distracteurs absurdes, hors-sujet, ou bonne réponse identifiable par longueur seule

4. **niveau_cert** : le niveau est-il bien celui d'un consultant fonctionnel Odoo ? Pas de question dev (code Python, ORM, XML), pas de question trivia trop générale.
   - 5 : question typique d'un examen fonctionnel
   - 3 : un peu trop facile OU un peu trop technique (frôle la limite dev)
   - 1 : question dev pure OU trivia hors-scope cert

5. **pertinence_module** : la question porte-t-elle bien sur le module annoncé (`module` du chunk) et pas sur un module voisin par confusion ?
   - 5 : question 100 % dans le scope du module
   - 3 : question légèrement transverse (touche un autre module au passage)
   - 1 : la question relève en réalité d'un autre module

# Format de sortie

Array JSON ordonné dans le MÊME ordre que les questions reçues (1 entrée par question) :

```
[
  {
    "factualite": 1-5,
    "clarte": 1-5,
    "distracteurs": 1-5,
    "niveau_cert": 1-5,
    "pertinence_module": 1-5,
    "reasons": ["raison courte critère bas 1", "raison courte critère bas 2"]
  },
  ...
]
```

Le champ `reasons` est OBLIGATOIRE. Il contient 1 à 3 raisons courtes (max 100 chars chacune) qui expliquent les notes les plus basses. Si toutes les notes sont à 5, mets `["RAS"]`.

# Exemples de notation

**Exemple A — bonne question (toutes notes 5)**

Question : "When configuring a self-ordering kiosk, what determines which menu is displayed?"
Bonne réponse : "The Point of Sale session linked to the QR code's source"
Distracteurs équilibrés sur le même module (PoS), longueurs comparables, basés sur le chunk.
→ `{"factualite":5,"clarte":5,"distracteurs":5,"niveau_cert":5,"pertinence_module":5,"reasons":["RAS"]}`

**Exemple B — distracteur faible**

Question : "Where is the partner email shown in the CRM lead form?"
Bonne réponse : "In the contact panel on the right side"
Distracteurs : "In the chatter", "Nowhere", "In the URL"
Le distracteur "Nowhere" est trivialement éliminable → distracteurs=2.
→ `{"factualite":5,"clarte":5,"distracteurs":2,"niveau_cert":4,"pertinence_module":5,"reasons":["distracteur 'Nowhere' trivialement éliminable"]}`

**Exemple C — bonne réponse trop longue (triche par longueur)**

Question : "What is the role of a serial number in inventory tracking?"
Bonne réponse : "A serial number uniquely identifies a single physical unit of a product, allowing Odoo to track its movements, expiration, and warranty per individual item across the entire stock journey from reception to delivery"
Distracteurs : "An internal reference", "A purchase code", "A vendor tag"
La bonne réponse est 4× plus longue que les distracteurs → reconnaissable au coup d'œil → distracteurs=1 ou 2.
→ `{"factualite":5,"clarte":4,"distracteurs":1,"niveau_cert":4,"pertinence_module":5,"reasons":["bonne réponse 4x plus longue → triche par longueur"]}`

**Exemple D — question dev (hors scope cert fonctionnelle)**

Question : "Which Python decorator is used to define a computed field?"
Bonne réponse : "@api.depends"
→ Question dev pure, pas cert fonctionnelle → niveau_cert=1.
→ `{"factualite":5,"clarte":5,"distracteurs":5,"niveau_cert":1,"pertinence_module":3,"reasons":["question développeur (Python ORM), pas cert fonctionnelle"]}`

**Exemple E — hallucination par rapport au chunk**

Question : "What's the default credit limit for new customers in Odoo CRM?"
Le chunk fourni parle de pipeline CRM, pas de credit limit (qui n'existe d'ailleurs pas en CRM mais en Accounting).
→ factualite=1 (info absente du chunk + module confondu) ET pertinence_module=1.
→ `{"factualite":1,"clarte":4,"distracteurs":3,"niveau_cert":3,"pertinence_module":1,"reasons":["info hors chunk fourni","CRM n'a pas de credit limit, c'est Accounting"]}`

# Règles de pilotage

- Sois exigeant. Ne mets 5 partout que si la question est réellement irréprochable.
- Une question avec un distracteur faible → distracteurs=3 au maximum, pas 5.
- Une question dont la bonne réponse est devinable par exclusion mécanique → clarte=2.
- Une question qui mêle 2 modules au sens cert (ex. "in CRM, what's the effect on Accounting…") → pertinence_module ≤ 3.
- Le `min` des 5 critères fait foi pour la décision finale, donc tu peux mettre 5 partout sauf 1 critère faible et la question sera marquée à juste titre.
- Toutes les questions du lot reçoivent un objet de note (jamais omettre une question, jamais en rajouter).
"""


# --- Lecture pending --------------------------------------------------------


def load_pending_files(pending_dir: Path) -> list[tuple[Path, list[dict]]]:
    """Liste les .jsonl pending et leurs questions. Path résolu absolu."""
    out: list[tuple[Path, list[dict]]] = []
    pending_dir = pending_dir.resolve()
    for p in sorted(pending_dir.glob("*.jsonl")):
        qs = [json.loads(line) for line in open(p, encoding="utf-8") if line.strip()]
        out.append((p.resolve(), qs))
    return out


def group_by_chunk(questions: list[dict]) -> dict[str, list[dict]]:
    """Groupe les questions par source_chunk_id pour amortir le contexte."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for q in questions:
        cid = q.get("source_chunk_id") or f"_orphan_{q.get('id')}"
        groups[cid].append(q)
    return groups


# --- Build batch requests --------------------------------------------------


def _format_chunk_block(q: dict) -> str:
    return (
        f"Module: {q.get('module', '?')}\n"
        f"Version: {q.get('target_version', '?')}\n"
        f"Source URL: {q.get('source_chunk_url', '?')}\n"
    )


def _format_question_block(q: dict, idx: int) -> str:
    opts_lines = []
    for a in q.get("answers", []):
        mark = "✓" if a.get("is_correct") else " "
        opts_lines.append(f"   [{mark}] {a.get('value', '')}")
    return (
        f"--- Question {idx} (qid={q.get('id')}) ---\n"
        f"Title (EN): {q.get('title', '')}\n"
        f"Title (FR): {q.get('title_fr', '')}\n"
        f"Options:\n" + "\n".join(opts_lines) + "\n"
        f"Evidence snippet (50-150 words from the chunk):\n"
        f"   {q.get('evidence_snippet', '')}\n"
        f"Difficulty: {q.get('difficulty', '?')} | Scenario-based: {q.get('scenario_based', '?')}\n"
    )


def build_judge_requests(
    pending_files: list[tuple[Path, list[dict]]],
    *,
    model: str,
) -> tuple[list[dict], dict[str, dict]]:
    """Build batch requests : 1 requête par groupe (chunk, fichier).

    Retourne (requests, mapping) — mapping custom_id → {file, qids[]}.
    """
    from scripts.generate_questions import _system_blocks

    requests: list[dict] = []
    mapping: dict[str, dict] = {}

    for file_path, qs in pending_files:
        file_slug = file_path.stem
        groups = group_by_chunk(qs)
        for chunk_id, group_qs in groups.items():
            qids = [q["id"] for q in group_qs]
            cid_str = chunk_id[:16] if chunk_id and not chunk_id.startswith("_orphan_") else chunk_id
            custom_id = f"{file_slug}__{cid_str}"
            ctx_header = _format_chunk_block(group_qs[0])
            qblocks = "\n".join(
                _format_question_block(q, i + 1) for i, q in enumerate(group_qs)
            )
            user = (
                f"{ctx_header}\n"
                f"Nombre de questions à juger : {len(group_qs)}\n\n"
                f"{qblocks}\n"
                f"Tâche : produire un array JSON de {len(group_qs)} entrées, "
                f"dans le MÊME ordre que les questions ci-dessus, conforme au "
                f"format imposé par le system."
            )
            params = {
                "model": model,
                "max_tokens": 2048,
                "system": _system_blocks(JUDGE_SYSTEM),
                "messages": [{"role": "user", "content": user}],
            }
            requests.append({"custom_id": custom_id, "params": params})
            # Stocker un path absolu pour éviter les problèmes de relative_to
            mapping[custom_id] = {
                "file": str(file_path),
                "qids": qids,
            }
    return requests, mapping


# --- Decision + apply ------------------------------------------------------


def _decision_from_scores(scores: dict) -> tuple[int, str, str]:
    """Retourne (score_min, decision, status)."""
    keys = ("factualite", "clarte", "distracteurs", "niveau_cert", "pertinence_module")
    values = [int(scores.get(k, 0) or 0) for k in keys]
    score = min(values) if values else 0
    if score >= 4:
        return score, "accept", "verified_by_judge"
    if score == 3:
        return score, "review", "unverified"
    return score, "reject", "flagged"


def apply_judge_results(
    results: list[dict],
    mapping: dict[str, dict],
) -> dict:
    """Parse les résultats du batch judge, met à jour les pending JSONL en place,
    et logue les rejets dans rejected_questions.jsonl.
    """
    from scripts.generate_questions import _parse_json_array

    stats = {
        "n_results": len(results),
        "n_ok": 0,
        "n_errored": 0,
        "n_parse_fail": 0,
        "n_count_mismatch": 0,
        "n_judged": 0,
        "n_accept": 0,
        "n_review": 0,
        "n_reject": 0,
        "usage_input_total": 0,
        "usage_output_total": 0,
        "usage_cache_read_total": 0,
    }

    # Build qid → judgment dict
    judgments: dict[int, dict] = {}

    for r in results:
        cid = r["custom_id"]
        meta = mapping.get(cid)
        if meta is None:
            stats["n_errored"] += 1
            continue
        if r["type"] != "succeeded":
            stats["n_errored"] += 1
            continue
        stats["n_ok"] += 1

        usage = r.get("usage") or {}
        stats["usage_input_total"] += int(usage.get("input_tokens", 0))
        stats["usage_output_total"] += int(usage.get("output_tokens", 0))
        stats["usage_cache_read_total"] += int(usage.get("cache_read_input_tokens", 0))

        text = r.get("text") or ""
        try:
            arr = _parse_json_array(text)
        except Exception as e:
            stats["n_parse_fail"] += 1
            print(f"  parse fail {cid}: {e}", file=sys.stderr)
            continue
        qids = meta["qids"]
        if len(arr) != len(qids):
            stats["n_count_mismatch"] += 1
            print(f"  count mismatch {cid}: judge={len(arr)} vs qids={len(qids)}",
                  file=sys.stderr)
            # On consomme dans l'ordre, max(min(len)) — sous-jugement plutôt que skip
        for i, qid in enumerate(qids):
            if i >= len(arr):
                break
            scores = arr[i]
            if not isinstance(scores, dict):
                continue
            score, decision, status = _decision_from_scores(scores)
            reasons_raw = scores.get("reasons") or []
            if not isinstance(reasons_raw, list):
                reasons_raw = [str(reasons_raw)]
            judgments[qid] = {
                "judge_score": score,
                "judge_decision": decision,
                "judge_reasons": [str(x)[:200] for x in reasons_raw],
                "status": status,
                "judge_scores_detail": {
                    k: int(scores.get(k, 0) or 0) for k in
                    ("factualite", "clarte", "distracteurs", "niveau_cert", "pertinence_module")
                },
            }
            stats["n_judged"] += 1
            if decision == "accept":
                stats["n_accept"] += 1
            elif decision == "review":
                stats["n_review"] += 1
            else:
                stats["n_reject"] += 1

    # Update pending files in place
    touched_files = set(m["file"] for m in mapping.values())
    rejected_log_lines: list[str] = []
    for f in sorted(touched_files):
        path = Path(f)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            continue
        qs = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
        for q in qs:
            jud = judgments.get(q.get("id"))
            if not jud:
                continue
            q["judge_score"] = jud["judge_score"]
            q["judge_decision"] = jud["judge_decision"]
            q["judge_reasons"] = jud["judge_reasons"]
            q["status"] = jud["status"]
            q["judge_scores_detail"] = jud["judge_scores_detail"]
            if jud["judge_decision"] == "reject":
                rejected_log_lines.append(json.dumps({
                    "qid": q.get("id"),
                    "module": q.get("module"),
                    "version": q.get("target_version"),
                    "title": q.get("title"),
                    "judge_score": jud["judge_score"],
                    "judge_reasons": jud["judge_reasons"],
                    "source_chunk_url": q.get("source_chunk_url"),
                    "logged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }, ensure_ascii=False))
        # Réécriture atomique
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for q in qs:
                f.write(json.dumps(q, ensure_ascii=False) + "\n")
        tmp.replace(path)

    if rejected_log_lines:
        REJECTED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(REJECTED_LOG, "a", encoding="utf-8") as f:
            for ln in rejected_log_lines:
                f.write(ln + "\n")
        stats["rejected_logged"] = len(rejected_log_lines)

    stats["files_touched"] = sorted(touched_files)
    return stats


# --- Cost estimate ----------------------------------------------------------


def estimate_judge_cost(n_requests: int) -> dict:
    tokens_in_first = 1 * EST_INPUT_TOKENS_FIRST
    tokens_in_cached = max(0, n_requests - 1) * EST_INPUT_TOKENS_CACHED
    tokens_out = n_requests * EST_OUTPUT_TOKENS_PER_CALL
    cost = (
        tokens_in_first * PRICE_INPUT_BASE_MT / 1e6
        + tokens_in_cached * PRICE_INPUT_CACHED_MT / 1e6
        + tokens_out * PRICE_OUTPUT_MT / 1e6
    )
    return {
        "n_requests": n_requests,
        "tokens_in_first": tokens_in_first,
        "tokens_in_cached": tokens_in_cached,
        "tokens_out": tokens_out,
        "cost_total_usd": round(cost, 2),
        "eta_min_low": max(5, n_requests // 20),
        "eta_min_high": max(30, n_requests // 10),
    }


# --- State -----------------------------------------------------------------


def _load_state() -> dict:
    if not JUDGE_STATE.exists():
        return {}
    try:
        return json.loads(JUDGE_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    JUDGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = JUDGE_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(JUDGE_STATE)


# --- Commandes -------------------------------------------------------------


def cmd_dry_run() -> int:
    pending = load_pending_files(PENDING_DIR)
    if not pending:
        print(f"❌ Aucun fichier dans {PENDING_DIR}", file=sys.stderr)
        return 1
    total_qs = sum(len(qs) for _, qs in pending)
    model = _answer_model()
    requests, mapping = build_judge_requests(pending, model=model)
    est = estimate_judge_cost(len(requests))
    print()
    print("=" * 70)
    print("JUDGE — Phase 5.5.b — DRY RUN")
    print("=" * 70)
    print(f"Pending files     : {len(pending)}")
    print(f"Questions à juger : {total_qs}")
    print(f"Appels Batch      : {est['n_requests']} (groupés par chunk → ~4 q/appel)")
    print()
    print("--- COÛT ESTIMÉ ---")
    print(f"Tokens IN  (first)  : {est['tokens_in_first']:>10,}")
    print(f"Tokens IN  (cached) : {est['tokens_in_cached']:>10,}")
    print(f"Tokens OUT          : {est['tokens_out']:>10,}")
    print(f"TOTAL               : ${est['cost_total_usd']:.2f}")
    print(f"ETA                 : {est['eta_min_low']}-{est['eta_min_high']} min")
    print()
    print(f"Modèle              : {model}")
    return 0


def cmd_submit() -> int:
    pending = load_pending_files(PENDING_DIR)
    if not pending:
        print(f"❌ Aucun fichier dans {PENDING_DIR}", file=sys.stderr)
        return 1
    model = _answer_model()
    requests, mapping = build_judge_requests(pending, model=model)
    if not requests:
        print("❌ Aucune requête.", file=sys.stderr)
        return 1
    import anthropic
    client = anthropic.Anthropic(api_key=_anthropic_key())
    print(f"→ Soumission batch judge ({len(requests)} requêtes)…")
    batch_id = submit_batch(client, requests)
    print(f"✓ Batch judge soumis : {batch_id}")

    state = _load_state()
    state[batch_id] = {
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_requests": len(requests),
        "model": model,
        "mapping": mapping,
        "status": "submitted",
    }
    _save_state(state)
    print(f"Pour poller : python3 -m scripts.judge_questions --poll {batch_id}")
    return 0


def cmd_poll(batch_id: str, *, interval_s: int = 30) -> int:
    state = _load_state()
    entry = state.get(batch_id)
    if entry is None:
        print(f"❌ batch_id {batch_id} absent de {JUDGE_STATE}", file=sys.stderr)
        return 1
    mapping = entry["mapping"]

    import anthropic
    client = anthropic.Anthropic(api_key=_anthropic_key())
    print(f"→ Poll batch judge {batch_id}…")
    poll_batch(client, batch_id, interval_s=interval_s)
    print("✓ Batch terminé. Download résultats…")
    results = download_results(client, batch_id)
    print(f"✓ {len(results)} résultats. Application…")
    stats = apply_judge_results(results, mapping)

    entry["status"] = "applied"
    entry["finished_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["stats"] = stats
    _save_state(state)

    print()
    print("=" * 70)
    print("JUDGE RÉSULTATS")
    print("=" * 70)
    print(f"Requests           : {entry['n_requests']}")
    print(f"OK / errored       : {stats['n_ok']} / {stats['n_errored']}")
    print(f"Parse fail         : {stats['n_parse_fail']}")
    print(f"Count mismatch     : {stats['n_count_mismatch']}")
    print(f"Questions jugées   : {stats['n_judged']}")
    print(f"  accept (≥4)      : {stats['n_accept']} "
          f"({stats['n_accept']/max(1,stats['n_judged'])*100:.1f} %)")
    print(f"  review (3)       : {stats['n_review']} "
          f"({stats['n_review']/max(1,stats['n_judged'])*100:.1f} %)")
    print(f"  reject (≤2)      : {stats['n_reject']} "
          f"({stats['n_reject']/max(1,stats['n_judged'])*100:.1f} %)")
    print()
    print(f"Tokens IN          : {stats['usage_input_total']:,}")
    print(f"Tokens IN cached   : {stats['usage_cache_read_total']:,}")
    print(f"Tokens OUT         : {stats['usage_output_total']:,}")
    cost = (
        stats['usage_input_total'] * PRICE_INPUT_BASE_MT / 1e6
        + stats['usage_cache_read_total'] * PRICE_INPUT_CACHED_MT / 1e6
        + stats['usage_output_total'] * PRICE_OUTPUT_MT / 1e6
    )
    print(f"Coût réel          : ${cost:.2f}")
    return 0


def cmd_run(interval_s: int) -> int:
    rc = cmd_submit()
    if rc != 0:
        return rc
    state = _load_state()
    latest = max(state.items(), key=lambda kv: kv[1].get("submitted_at", ""))
    return cmd_poll(latest[0], interval_s=interval_s)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--poll-interval", type=int, default=30)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--submit", action="store_true")
    group.add_argument("--poll", metavar="BATCH_ID")
    group.add_argument("--run", action="store_true")

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
