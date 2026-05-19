#!/usr/bin/env python3
"""Inspecte un fichier de questions générées en pending.

Usage :
  python3 -m scripts.inspect_pending                          # dernier .jsonl
  python3 -m scripts.inspect_pending --file <path>            # fichier précis
  python3 -m scripts.inspect_pending --sample 8 --seed 42     # n échantillon
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PENDING_DIR = ROOT / "data" / "generated_pending"


def _latest_pending() -> Path | None:
    files = sorted(PENDING_DIR.glob("*.jsonl"))
    return files[-1] if files else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, default=None)
    parser.add_argument("--sample", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    path = args.file or _latest_pending()
    if path is None or not path.exists():
        print("❌ Aucun fichier pending trouvé.", file=sys.stderr)
        return 1

    qs = [json.loads(line) for line in open(path, encoding="utf-8")]
    print(f"Fichier : {path}")
    print(f"Total valides en pending : {len(qs)}\n")

    rng = random.Random(args.seed)
    sample = rng.sample(qs, min(args.sample, len(qs)))

    for i, q in enumerate(sample, 1):
        qid = q["id"]
        diff = q.get("difficulty")
        scen = q.get("scenario_based")
        title = q["title"]
        title_fr = q.get("title_fr", "")
        answers = q.get("answers", [])
        snippet = q.get("evidence_snippet", "")
        n_words = len(snippet.split())
        explication = q.get("explication_claude", "")
        url = q.get("source_chunk_url", "")

        print(f"--- Question {i} (qid={qid}, difficulty={diff}, scenario={scen}) ---")
        print(f"EN: {title}")
        print(f"FR: {title_fr}")
        print("Options :")
        for a in answers:
            marker = "✓" if a.get("is_correct") else " "
            value = a.get("value", "")
            value_fr = a.get("value_fr", "")
            print(f"  [{marker}] {value}")
            print(f"       {value_fr}")
        print(f"\nEvidence ({n_words} mots) :")
        print(f"  {snippet}")
        print(f"\nExplication: {explication}")
        print(f"Source URL : {url}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
