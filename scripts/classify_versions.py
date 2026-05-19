#!/usr/bin/env python3
"""
Classification target_version (18.0 / 19.0 / both) via Claude + RAG doc v18/v19.

python3 -m scripts.classify_versions              # target_version NULL / vide seulement
python3 -m scripts.classify_versions --reclassify-all
python3 -m scripts.classify_versions --question-id 42
python3 -m scripts.classify_versions --dry-run --limit 20
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUESTIONS_PATH = ROOT / "questions.json"
REVIEW_CSV = ROOT / "data" / "classification_review.csv"
BATCH_SIZE = 5
SAVE_EVERY = 10

CSV_FIELDS = [
    "question_id",
    "title",
    "proposed_target_version",
    "confiance",
    "raisonnement",
    "error",
]


@dataclass
class RowOutcome:
    question_id: int | str
    title: str
    applied: bool
    review: bool
    target_version: str | None
    confiance: str | None
    raisonnement: str | None
    error: str | None = None


def _load_questions() -> tuple[dict, list[dict]]:
    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    qs = data.get("questions")
    if not isinstance(qs, list):
        raise ValueError("questions.json : clé 'questions' invalide")
    return data, qs


def _backup_questions() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = QUESTIONS_PATH.with_suffix(f".json.bak.{stamp}")
    shutil.copy2(QUESTIONS_PATH, backup)
    return backup


def _save_questions(data: dict) -> None:
    with open(QUESTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _find_question_index(questions: list[dict], q_id: int) -> int | None:
    for i, q in enumerate(questions):
        try:
            if int(q.get("id")) == q_id:
                return i
        except (TypeError, ValueError):
            continue
    return None


def _select_questions(
    questions: list[dict],
    *,
    reclassify_all: bool,
    question_id: int | None,
    limit: int | None,
) -> list[dict]:
    from app.classify import is_unclassified

    if question_id is not None:
        idx = _find_question_index(questions, question_id)
        if idx is None:
            raise SystemExit(f"Question id {question_id} introuvable.")
        return [questions[idx]]

    if reclassify_all:
        pool = [q for q in questions if isinstance(q, dict)]
    else:
        pool = [q for q in questions if isinstance(q, dict) and is_unclassified(q)]

    if limit is not None and limit > 0:
        pool = pool[:limit]
    return pool


def _append_review_rows(rows: list[RowOutcome], *, dry_run: bool) -> None:
    if dry_run or not rows:
        return
    REVIEW_CSV.parent.mkdir(parents=True, exist_ok=True)
    write_header = not REVIEW_CSV.exists() or REVIEW_CSV.stat().st_size == 0
    with open(REVIEW_CSV, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            w.writeheader()
        for r in rows:
            if not r.review:
                continue
            w.writerow(
                {
                    "question_id": r.question_id,
                    "title": r.title[:500],
                    "proposed_target_version": r.target_version or "",
                    "confiance": r.confiance or "",
                    "raisonnement": (r.raisonnement or "")[:2000],
                    "error": r.error or "",
                }
            )


def _apply_outcome(q: dict, outcome: RowOutcome) -> None:
    if outcome.applied and outcome.target_version:
        q["target_version"] = outcome.target_version
    elif outcome.review:
        q["target_version"] = None


def _classify_sync(q: dict) -> RowOutcome:
    from app.classify import classify_question, correct_answer_text, option_texts

    qid = q.get("id")
    title = (q.get("title") or "")[:500]
    try:
        if not option_texts(q):
            raise ValueError("options vides")
        if not correct_answer_text(q):
            raise ValueError("pas de bonne réponse is_correct")

        result, _meta = classify_question(q)
        conf = result.confiance
        tv = result.target_version
        raison = result.raisonnement
        if conf == "haute":
            return RowOutcome(
                question_id=qid,
                title=title,
                applied=True,
                review=False,
                target_version=tv,
                confiance=conf,
                raisonnement=raison,
            )
        return RowOutcome(
            question_id=qid,
            title=title,
            applied=False,
            review=True,
            target_version=tv,
            confiance=conf,
            raisonnement=raison,
        )
    except Exception as e:
        return RowOutcome(
            question_id=qid,
            title=title,
            applied=False,
            review=True,
            target_version=None,
            confiance=None,
            raisonnement=None,
            error=str(e),
        )


async def _classify_one(q: dict, sem: asyncio.Semaphore) -> RowOutcome:
    async with sem:
        return await asyncio.to_thread(_classify_sync, q)


async def _run_batch(questions: list[dict], dry_run: bool) -> list[RowOutcome]:
    sem = asyncio.Semaphore(BATCH_SIZE)
    tasks = [_classify_one(q, sem) for q in questions]
    try:
        from tqdm.asyncio import tqdm_asyncio

        return await tqdm_asyncio.gather(*tasks, desc="Classification")
    except ImportError:
        from tqdm import tqdm

        outcomes: list[RowOutcome] = []
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Classification"):
            outcomes.append(await coro)
        return outcomes


def _print_summary(outcomes: list[RowOutcome], *, dry_run: bool) -> None:
    applied = {"18.0": 0, "19.0": 0, "both": 0}
    review = 0
    errors = 0
    for o in outcomes:
        if o.error:
            errors += 1
        if o.review:
            review += 1
        if o.applied and o.target_version in applied:
            applied[o.target_version] += 1

    mode = "DRY-RUN" if dry_run else "écrit"
    print(f"\n=== Résumé ({mode}) ===")
    print(f"  Traitées     : {len(outcomes)}")
    print(f"  Appliquées   : both={applied['both']}  18.0={applied['18.0']}  19.0={applied['19.0']}")
    print(f"  Revue manuelle (CSV) : {review}")
    print(f"  Erreurs API  : {errors}")
    if not dry_run and review:
        print(f"  CSV : {REVIEW_CSV}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Classification target_version questions")
    parser.add_argument(
        "--reclassify-all",
        action="store_true",
        help="Reclassifier toutes les questions (sinon seulement target_version NULL/vide)",
    )
    parser.add_argument("--question-id", type=int, default=None, help="Une seule question par id")
    parser.add_argument("--dry-run", action="store_true", help="Aucune écriture JSON/CSV")
    parser.add_argument("--limit", type=int, default=None, help="Limiter le nombre de questions")
    args = parser.parse_args()

    from app.classify import is_unclassified
    from app.llm import api_available

    if not api_available():
        print("❌ Clé API Anthropic absente (config.json → anthropic.api_key).", file=sys.stderr)
        return 1

    data, all_qs = _load_questions()
    try:
        batch = _select_questions(
            all_qs,
            reclassify_all=args.reclassify_all,
            question_id=args.question_id,
            limit=args.limit,
        )
    except SystemExit as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    if not batch:
        if args.reclassify_all:
            print("Aucune question à traiter.")
        else:
            n_uncl = sum(1 for q in all_qs if isinstance(q, dict) and is_unclassified(q))
            print(
                f"Aucune question non classée (NULL/vide). "
                f"Toutes ont déjà un target_version ({len(all_qs) - n_uncl} classées). "
                f"Utilisez --reclassify-all pour tout refaire."
            )
        return 0

    print(f"Questions à classifier : {len(batch)}")
    if args.dry_run:
        print("(dry-run — aucune écriture)")

    backup: Path | None = None
    if not args.dry_run:
        backup = _backup_questions()
        print(f"Sauvegarde : {backup}")

    outcomes = asyncio.run(_run_batch(batch, args.dry_run))

    # Appliquer sur les objets question
    id_to_q = {q.get("id"): q for q in batch}
    review_rows: list[RowOutcome] = []
    pending_save = 0
    for i, outcome in enumerate(outcomes):
        q = id_to_q.get(outcome.question_id)
        if q is None:
            continue
        if not args.dry_run:
            _apply_outcome(q, outcome)
            pending_save += 1
            if outcome.review:
                review_rows.append(outcome)
            if pending_save >= SAVE_EVERY:
                _save_questions(data)
                _append_review_rows(review_rows, dry_run=False)
                review_rows.clear()
                pending_save = 0
        elif outcome.review or outcome.error:
            review_rows.append(outcome)

    if not args.dry_run:
        _save_questions(data)
        _append_review_rows(review_rows, dry_run=False)
        from app.odoo_docs_rag import invalidate_doc_index_cache

        invalidate_doc_index_cache()
    else:
        _append_review_rows(review_rows, dry_run=True)
        if review_rows:
            print(f"(dry-run) {len(review_rows)} ligne(s) auraient été ajoutées à {REVIEW_CSV}")

    _print_summary(outcomes, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
