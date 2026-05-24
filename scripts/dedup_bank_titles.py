#!/usr/bin/env python3
"""Dédoublonne la banque par titre normalisé.

Pour chaque groupe de questions partageant la même clé de titre (la clé de doublon
utilisée par l'app), garde la version la plus complète (réponse correcte présente,
version taguée, explications, module) et retire les autres. Backup + dry-run.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from import_udemy import norm_title_key
except Exception:
    import re
    import unicodedata

    def norm_title_key(t: str) -> str:
        t = unicodedata.normalize("NFKD", (t or "")).encode("ascii", "ignore").decode()
        t = re.sub(r"\s+", " ", t.lower()).strip()
        return re.sub(r"[^\w ]", "", t)


def _score(q: dict) -> int:
    s = 0
    if any(a.get("is_correct") for a in (q.get("answers") or [])):
        s += 8
    tv = q.get("target_version")
    if tv and str(tv).strip():
        s += 4
    if (q.get("explication_claude") or "").strip() or (q.get("explication_senedoo") or "").strip():
        s += 2
    if (q.get("module") or "").strip():
        s += 1
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(ROOT / "questions.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    qpath = Path(args.questions)
    data = json.loads(qpath.read_text(encoding="utf-8"))
    questions = data["questions"]

    groups: dict[str, list[dict]] = defaultdict(list)
    for q in questions:
        k = norm_title_key(q.get("title") or "")
        if k:
            groups[k].append(q)

    remove = set()
    examples = []
    for grp in groups.values():
        if len(grp) < 2:
            continue
        grp_sorted = sorted(
            grp, key=lambda q: (-_score(q), q.get("id") if isinstance(q.get("id"), int) else 0)
        )
        keep = grp_sorted[0]
        for q in grp_sorted[1:]:
            remove.add(id(q))
        if len(examples) < 6:
            examples.append((keep.get("title", "")[:60], len(grp),
                             keep.get("id"), [q.get("id") for q in grp_sorted[1:]]))

    new_questions = [q for q in questions if id(q) not in remove]
    removed = len(questions) - len(new_questions)
    print(f"groupes en double : {sum(1 for g in groups.values() if len(g) > 1)} | "
          f"questions retirées : {removed} | total après : {len(new_questions)}")
    for t, n, kid, rids in examples:
        print(f"  gardé id {kid} (1/{n}), retire {rids} : {t}")

    if args.dry_run:
        print("DRY-RUN — rien écrit.")
        return
    if not removed:
        print("Rien à retirer.")
        return
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bak = qpath.with_name(qpath.name + f".bak.{ts}")
    bak.write_text(qpath.read_text(encoding="utf-8"), encoding="utf-8")
    data["questions"] = new_questions
    tmp = qpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(qpath)
    print(f"Retiré {removed}. Total : {len(new_questions)}. Backup : {bak.name}")


if __name__ == "__main__":
    main()
