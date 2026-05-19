#!/usr/bin/env python3
"""CLI : revue des suggestions à faible confiance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "suggestions.log"


def _load_questions_by_id() -> dict[int | str, dict]:
    qpath = ROOT / "questions.json"
    if not qpath.exists():
        return {}
    with open(qpath, encoding="utf-8") as f:
        raw = json.load(f)
    bank = raw.get("questions") if isinstance(raw, dict) else raw
    if not isinstance(bank, list):
        return {}
    return {q.get("id"): q for q in bank if isinstance(q, dict) and q.get("id") is not None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Suggestions Claude à revoir")
    parser.add_argument(
        "--confidence",
        default="basse",
        choices=("basse", "moyenne", "haute"),
        help="Filtrer par niveau de confiance (défaut : basse)",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--version",
        default=None,
        help="Filtrer par target_version loguée (ex. 18.0, 19.0)",
    )
    args = parser.parse_args(argv)

    if not LOG_PATH.exists():
        print(f"Aucun journal : {LOG_PATH}")
        return 0

    bank = _load_questions_by_id()
    shown = 0
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (row.get("confiance") or "").strip().lower() != args.confidence:
                continue
            if args.version and (row.get("target_version") or "").strip() != args.version.strip():
                continue
            qid = row.get("question_id")
            q = bank.get(qid) or {}
            title = (q.get("title") or "")[:120]
            srcs = row.get("sources") or []
            refs = ", ".join(
                f"{s.get('type')}:{s.get('ref')}" for s in srcs[:3] if isinstance(s, dict)
            )
            print(
                f"- {row.get('timestamp')} id={qid} lat={row.get('latency_s')}s "
                f"model={row.get('model')}\n  {title}\n  sources: {refs or '—'}"
            )
            shown += 1
            if shown >= args.limit:
                break
    print(f"\n{shown} entrée(s) confiance={args.confidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
