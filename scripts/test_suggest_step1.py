#!/usr/bin/env python3
"""Test étape 1 : JSON valide sur N questions (nécessite config.json + clé API)."""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.llm import api_available, suggest_answer
    from app.rag import build_context

    if not api_available():
        print("SKIP: pas de clé anthropic.api_key dans config.json")
        return 0

    qpath = ROOT / "questions.json"
    with open(qpath, encoding="utf-8") as f:
        raw = json.load(f)
    bank = raw.get("questions") if isinstance(raw, dict) else raw
    if not isinstance(bank, list):
        print("questions.json invalide")
        return 1

    sample = random.sample(bank, min(5, len(bank)))
    ok = 0
    for q in sample:
        title = (q.get("title") or "").strip()
        opts = []
        for a in q.get("answers") or []:
            if isinstance(a, dict):
                v = (a.get("value") or "").strip()
            else:
                v = str(a or "").strip()
            if v:
                opts.append(v)
        ctx = build_context(title, opts, question_bank=bank, target_version="18.0")
        try:
            sug, meta = suggest_answer(title, opts, ctx, question_id=q.get("id"))
            print(
                f"OK id={q.get('id')} confiance={sug.confiance} reponse={sug.reponse!r} "
                f"retry={meta.get('retried_json')}"
            )
            ok += 1
        except Exception as e:
            print(f"FAIL id={q.get('id')}: {e}")
    print(f"\nRésultat : {ok}/{len(sample)} JSON valides")
    return 0 if ok >= min(4, len(sample)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
