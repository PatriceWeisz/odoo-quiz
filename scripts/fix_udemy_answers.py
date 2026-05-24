#!/usr/bin/env python3
"""Corrige la bonne réponse des questions Udemy de la banque à partir d'un export
de cours Udemy (réponses vérifiées humain).

Pour chaque question de l'export qui existe déjà en banque (même titre normalisé)
et dont la bonne réponse diffère, on repère dans la banque l'option dont le TEXTE
correspond à la bonne réponse vérifiée et on bascule `is_correct` dessus. Si aucune
option de la banque ne correspond (libellés trop différents), on saute et on signale
— on ne devine jamais.

Ne touche que les questions banque de source `udemy`.

Usage :
    python3 -m scripts.fix_udemy_answers data/udemy_new_course.json --dry-run
    python3 -m scripts.fix_udemy_answers data/udemy_new_course.json
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from import_udemy import norm_title_key
except Exception:
    import unicodedata

    def norm_title_key(t: str) -> str:
        t = unicodedata.normalize("NFKD", (t or "")).encode("ascii", "ignore").decode()
        t = re.sub(r"\s+", " ", t.lower()).strip()
        return re.sub(r"[^\w ]", "", t)


def _na(s: str) -> str:
    """Normalise un texte de réponse pour comparaison (unescape + minuscule + espaces)."""
    s = html.unescape(str(s or ""))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return re.sub(r"[^\w ]", "", s)


def _bank_src(q: dict) -> str:
    return str((q.get("correct_answer_source") or q.get("source") or "")).strip().lower()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("--questions", default=str(ROOT / "questions.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = json.loads(Path(args.infile).read_text(encoding="utf-8"))
    items = src.get("items") if isinstance(src, dict) else src

    qpath = Path(args.questions)
    data = json.loads(qpath.read_text(encoding="utf-8"))
    questions = data["questions"]

    key2q: dict[str, dict] = {}
    for q in questions:
        if _bank_src(q) != "udemy":
            continue
        k = norm_title_key(q.get("title") or "")
        if k:
            key2q.setdefault(k, q)

    corrected = already_ok = unmappable = no_bank = 0
    examples = []
    for it in items:
        title = html.unescape((it.get("question") or "").strip())
        k = norm_title_key(title)
        q = key2q.get(k)
        if not q:
            no_bank += 1
            continue
        ci = it.get("correct_index")
        ans = [str(a) for a in (it.get("answers") or [])]
        if not isinstance(ci, int) or ci < 1 or ci > len(ans):
            continue
        new_correct = _na(ans[ci - 1])
        bank_answers = q.get("answers") or []
        cur_correct = next((_na(a.get("value")) for a in bank_answers if a.get("is_correct")), None)
        if cur_correct == new_correct:
            already_ok += 1
            continue
        # chercher l'option banque correspondant au texte de la bonne réponse vérifiée
        match_idx = next((i for i, a in enumerate(bank_answers) if _na(a.get("value")) == new_correct), None)
        if match_idx is None:
            unmappable += 1
            if len(examples) < 6:
                examples.append({"id": q.get("id"), "title": title[:70],
                                 "verified": ans[ci - 1][:60], "bank_opts": [(_na(a.get('value'))[:40]) for a in bank_answers]})
            continue
        for i, a in enumerate(bank_answers):
            a["is_correct"] = (i == match_idx)
        q["correct_answer_source"] = "udemy"
        corrected += 1

    print(f"corrigées : {corrected} | déjà OK : {already_ok} | non mappables (sautées) : {unmappable} | hors banque : {no_bank}")
    if examples:
        print("exemples non mappables :")
        for e in examples:
            print("  -", e["id"], e["title"], "| vérif:", e["verified"])

    if args.dry_run:
        print("DRY-RUN — rien écrit.")
        return
    if not corrected:
        print("Aucune correction à appliquer.")
        return
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bak = qpath.with_name(qpath.name + f".bak.{ts}")
    bak.write_text(qpath.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = qpath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(qpath)
    print(f"Écrit {corrected} corrections. Backup : {bak.name}")


if __name__ == "__main__":
    main()
