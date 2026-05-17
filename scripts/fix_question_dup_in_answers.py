#!/usr/bin/env python3
"""Retire les réponses dont le texte recopie le titre de la question (questions.json)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from import_udemy import answer_duplicates_question_text, strip_question_duplicate_answers

try:
    import generate_explanations as ge
except ImportError:
    ge = None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Écrire questions.json (sinon affichage seulement).",
    )
    args = ap.parse_args()

    if ge is None:
        print("generate_explanations.py introuvable.", file=sys.stderr)
        return 1

    data = ge.load()
    questions = data.get("questions", [])
    if not isinstance(questions, list):
        print("questions.json invalide.", file=sys.stderr)
        return 1

    fixes: list[tuple[int, str, list[int], int, int]] = []
    for q in questions:
        qid = q.get("id")
        before = len(q.get("answers") or [])
        fixed, removed = strip_question_duplicate_answers(q)
        after = len(fixed.get("answers") or [])
        if removed:
            title = (q.get("title") or "")[:90]
            fixes.append((qid, title, removed, before, after))
            if args.apply:
                q.clear()
                q.update(fixed)

    print(f"Questions analysées : {len(questions)}")
    print(f"Fiches à corriger : {len(fixes)}")
    for qid, title, removed, before, after in fixes:
        print(f"  id={qid} : {before} → {after} réponse(s), indices retirés {removed}")
        print(f"    {title}…")

    if not fixes:
        print("Aucune correction nécessaire.")
        return 0

    if not args.apply:
        print("\nRelancez avec --apply pour enregistrer.")
        return 0

    ge.save(data)
    print(f"\n✅ {len(fixes)} fiche(s) corrigée(s) dans questions.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
