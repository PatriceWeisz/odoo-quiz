#!/usr/bin/env python3
"""Importe dans la banque les questions d'un cours Udemy (export API).

Entrée  : JSON { "items": [ {quiz, id, question, answers[], correct_index, section} ] }
          (export récupéré depuis l'API Udemy du cours, réponses vérifiées humain).
Action  : convertit au format banque (source=udemy), tague target_version + module
          (mappé depuis la section Udemy), dédoublonne par titre normalisé contre la
          banque existante ET au sein du lot, puis sauvegarde atomique (+ backup).

Usage :
    python3 -m scripts.import_udemy_course data/udemy_new_course.json --dry-run
    python3 -m scripts.import_udemy_course data/udemy_new_course.json
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from import_udemy import norm_title_key  # même clé de doublon que l'app
except Exception:  # repli autonome si import lourd indisponible
    import re
    import unicodedata

    def norm_title_key(t: str) -> str:
        t = unicodedata.normalize("NFKD", (t or "")).encode("ascii", "ignore").decode()
        t = re.sub(r"\s+", " ", t.lower()).strip()
        return re.sub(r"[^\w ]", "", t)

# Section Udemy -> module d'étude (app/study_modules.py). Sections ambiguës
# (Introduction, et toute section non listée) : laissées à l'inférence kNN.
SECTION_TO_MODULE = {
    "Sales": "sales",
    "Survey": "marketing/surveys",
    "CRM": "crm",
    "Marketing": "marketing/email_marketing",
    "Purchase": "purchase",
    "Studio": "studio",
    "Website": "websites/website",
    "Timesheet": "services/timesheets",
    "POS": "sales/point_of_sale",
    "HR": "hr",
    "eCommerce": "websites/ecommerce",
    "Knowledge": "productivity/knowledge",
    "Project": "services/project",
    "Accounting": "accounting",
    "Inventory": "inventory_and_mrp/inventory",
    "AI": "productivity/ai",
    "MRP": "inventory_and_mrp/manufacturing",
    "Spreadsheet": "productivity/spreadsheet",
    "Event": "marketing/events",
}


def _clean(s: str) -> str:
    return html.unescape((s or "").strip())


def main() -> None:
    ap = argparse.ArgumentParser(description="Import questions Udemy -> banque.")
    ap.add_argument("infile")
    ap.add_argument("--target-version", default="19.0")
    ap.add_argument("--questions", default=str(ROOT / "questions.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = json.loads(Path(args.infile).read_text(encoding="utf-8"))
    items = src.get("items") if isinstance(src, dict) else src
    if not isinstance(items, list):
        sys.exit("Format d'entrée invalide (clé 'items' attendue).")

    qpath = Path(args.questions)
    data = json.loads(qpath.read_text(encoding="utf-8"))
    questions = data["questions"]

    existing_keys = {norm_title_key(q.get("title") or "") for q in questions}
    existing_keys.discard("")
    max_qid = max((q["id"] for q in questions if isinstance(q.get("id"), int)), default=0)
    max_aid = max((a.get("id", 0) for q in questions for a in (q.get("answers") or [])
                   if isinstance(a.get("id"), int)), default=0)

    to_add: list[dict] = []
    seen_new: set[str] = set()
    skipped_dup = skipped_bad = 0

    for it in items:
        title = _clean(it.get("question"))
        answers = [_clean(a) for a in (it.get("answers") or [])]
        ci = it.get("correct_index")
        if (not title or len([a for a in answers if a]) < 2
                or not isinstance(ci, int) or ci < 1 or ci > len(answers)):
            skipped_bad += 1
            continue
        key = norm_title_key(title)
        if not key or key in existing_keys or key in seen_new:
            skipped_dup += 1
            continue
        seen_new.add(key)
        max_qid += 1
        ans_objs = []
        for i, a in enumerate(answers):
            max_aid += 1
            ans_objs.append({"id": max_aid, "value": a, "value_fr": "",
                             "is_correct": (i + 1 == ci), "score": 0.0})
        section = (it.get("section") or "").strip()
        q = {
            "id": max_qid,
            "title": title,
            "title_fr": "",
            "type": "simple_choice",
            "is_scored": True,
            "target_version": args.target_version,
            "correct_answer_source": "udemy",
            "explication_claude": "",
            "explication_senedoo": "",
            "topic": section or None,
            "answers": ans_objs,
        }
        mod = SECTION_TO_MODULE.get(section)
        if mod:
            q["module"] = mod
        to_add.append(q)

    print(f"Lot : {len(items)} | à ajouter : {len(to_add)} | doublons ignorés : "
          f"{skipped_dup} | invalides : {skipped_bad}")
    print("Par module :", dict(Counter(q.get("module", "(inféré)") for q in to_add)))

    if args.dry_run:
        print("DRY-RUN — rien écrit.")
        return
    if not to_add:
        print("Rien à insérer.")
        return

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bak = qpath.with_name(qpath.name + f".bak.{ts}")
    bak.write_text(qpath.read_text(encoding="utf-8"), encoding="utf-8")
    questions.extend(to_add)
    tmp = qpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(qpath)
    print(f"Inséré {len(to_add)}. Total banque : {len(questions)}. Backup : {bak.name}")


if __name__ == "__main__":
    main()
