#!/usr/bin/env python3
"""Calibrage style : statistiques sur les questions Udemy existantes.

Phase 5.2 du briefing. Sert à dimensionner la génération Claude :
  - longueurs typiques de titre / options / explications
  - distribution # options (3 vs 4)
  - patterns récurrents (scénario, négation, "all of the above"…)
  - distribution par module inféré (RAG sur title vers odoo_docs.sqlite)
  - couverture cert v18 / v19

Usage :
  python3 -m scripts.analyze_udemy_calibration
  python3 -m scripts.analyze_udemy_calibration --output data/calibration_report.md
  python3 -m scripts.analyze_udemy_calibration --infer-modules  # plus lent (RAG)
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.study_modules import all_modules, tier_of  # noqa: E402

DEFAULT_REPORT = ROOT / "data" / "calibration_report.md"
QUESTIONS_FILE = ROOT / "questions.json"


# --- Heuristiques de détection -----------------------------------------------

SCENARIO_PATTERNS = (
    r"\byou\s+(want|need|are|must|have)\b",
    r"\bif\s+you\b",
    r"\bwhen\s+(you|a|the)\b",
    r"\bsuppose\b",
    r"\bgiven\s+that\b",
    r"\bcompany\b.*\b(want|need|require)",
)
NEGATION_PATTERNS = (
    r"\bnot\b",
    r"\bcannot\b",
    r"\bnever\b",
    r"\bexcept\b",
    r"\bfalse\b",
)
ALL_OF_THE_ABOVE = (
    r"all of the above",
    r"toutes les r[ée]ponses",
    r"none of the above",
    r"aucune des r[ée]ponses",
)


def _match_any(text: str, patterns: tuple[str, ...]) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in patterns)


def _word_count(s: str) -> int:
    return len(re.findall(r"\w+", s or ""))


# --- Stats helpers -----------------------------------------------------------


def _stats(values: list[int | float]) -> dict[str, float]:
    if not values:
        return {"n": 0, "min": 0, "max": 0, "median": 0, "mean": 0.0, "p10": 0, "p90": 0}
    s = sorted(values)
    n = len(s)
    return {
        "n": n,
        "min": s[0],
        "max": s[-1],
        "median": s[n // 2],
        "mean": round(statistics.mean(s), 2),
        "p10": s[max(0, int(n * 0.1))],
        "p90": s[min(n - 1, int(n * 0.9))],
    }


def _hist(values: list[int], bins: list[tuple[int, int]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for lo, hi in bins:
        label = f"{lo}-{hi}"
        out[label] = sum(1 for v in values if lo <= v <= hi)
    return out


# --- RAG inférence module (optionnel) ----------------------------------------


def infer_module_for_title(title: str, top_chunks: int = 3) -> str | None:
    """Top chunk RAG → premier segment du path applications/ = module."""
    try:
        from app.odoo_docs_rag import search_doc_chunks
    except ImportError:
        return None
    chunks = search_doc_chunks(title, top_n=top_chunks, min_score=0.30)
    if not chunks:
        return None
    # Vote majoritaire pondéré par score
    counter: Counter[str] = Counter()
    for ch in chunks:
        url = ch.get("url", "")
        m = re.search(r"/applications/([^/]+(?:/[^/.]+)?)", url)
        if m:
            counter[m.group(1).replace(".html", "")] += ch.get("score") or 0
    if not counter:
        return None
    return counter.most_common(1)[0][0]


# --- Main analyse ------------------------------------------------------------


def load_udemy_questions() -> list[dict]:
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return [
        q for q in data.get("questions", [])
        if isinstance(q, dict) and q.get("correct_answer_source") == "udemy"
    ]


def analyze(questions: list[dict], *, infer_modules: bool = False) -> dict:
    n_total = len(questions)
    title_lens: list[int] = []
    n_options: list[int] = []
    option_lens: list[int] = []
    correct_lens: list[int] = []
    distractor_lens: list[int] = []
    scenario_count = 0
    negation_count = 0
    aota_count = 0
    target_versions: Counter[str] = Counter()
    n_correct_distrib: Counter[int] = Counter()
    has_fr_title = 0
    has_fr_value = 0
    has_explication_claude = 0
    has_image = 0
    inferred_modules: Counter[str] = Counter()
    inferred_tier: Counter[str] = Counter()

    for q in questions:
        title = q.get("title") or ""
        title_lens.append(_word_count(title))
        if (q.get("title_fr") or "").strip():
            has_fr_title += 1
        if (q.get("explication_claude") or "").strip():
            has_explication_claude += 1
        if (q.get("question_image") or "").strip():
            has_image += 1
        target_versions[str(q.get("target_version") or "null")] += 1

        if _match_any(title, SCENARIO_PATTERNS):
            scenario_count += 1
        if _match_any(title, NEGATION_PATTERNS):
            negation_count += 1
        if any(_match_any(a.get("value") or "", ALL_OF_THE_ABOVE) for a in q.get("answers") or []):
            aota_count += 1

        ans = q.get("answers") or []
        n_options.append(len(ans))
        n_correct = sum(1 for a in ans if a.get("is_correct"))
        n_correct_distrib[n_correct] += 1
        for a in ans:
            wlen = _word_count(a.get("value") or "")
            option_lens.append(wlen)
            if (a.get("value_fr") or "").strip():
                has_fr_value += 1
            if a.get("is_correct"):
                correct_lens.append(wlen)
            else:
                distractor_lens.append(wlen)

        if infer_modules:
            mod = infer_module_for_title(title)
            if mod:
                inferred_modules[mod] += 1
                t = tier_of(mod)
                if t:
                    inferred_tier[t] += 1
                else:
                    inferred_tier["(hors scope)"] += 1
            else:
                inferred_modules["(non classé)"] += 1
                inferred_tier["(non classé)"] += 1

    return {
        "n_total": n_total,
        "title_words": _stats(title_lens),
        "title_words_hist": _hist(
            title_lens, [(1, 8), (9, 14), (15, 20), (21, 30), (31, 50), (51, 200)]
        ),
        "n_options": dict(Counter(n_options)),
        "n_correct_distrib": dict(n_correct_distrib),
        "option_words": _stats(option_lens),
        "correct_option_words": _stats(correct_lens),
        "distractor_words": _stats(distractor_lens),
        "scenario_count": scenario_count,
        "negation_count": negation_count,
        "all_of_the_above_count": aota_count,
        "target_versions": dict(target_versions),
        "has_fr_title": has_fr_title,
        "has_fr_value": has_fr_value,
        "has_explication_claude": has_explication_claude,
        "has_image": has_image,
        "inferred_modules": dict(inferred_modules) if infer_modules else None,
        "inferred_tier": dict(inferred_tier) if infer_modules else None,
    }


def render_markdown(stats: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n = stats["n_total"]
    lines = [
        "# Calibrage style — questions Udemy",
        "",
        f"*Généré le {now} — n = {n} questions Udemy*",
        "",
        "## Longueurs",
        "",
        "**Titres** (en mots) :",
        f"- min/p10/median/mean/p90/max : "
        f"{stats['title_words']['min']} / {stats['title_words']['p10']} / "
        f"{stats['title_words']['median']} / {stats['title_words']['mean']} / "
        f"{stats['title_words']['p90']} / {stats['title_words']['max']}",
        "",
        "Histogramme :",
        "",
    ]
    for label, count in stats["title_words_hist"].items():
        bar = "█" * max(1, int(50 * count / max(1, n)))
        lines.append(f"- {label:>5} mots : `{bar}` {count}")
    lines.extend([
        "",
        "**Options** (en mots) :",
        f"- toutes options : median={stats['option_words']['median']}, "
        f"mean={stats['option_words']['mean']}, max={stats['option_words']['max']}",
        f"- bonne réponse  : median={stats['correct_option_words']['median']}, "
        f"mean={stats['correct_option_words']['mean']}",
        f"- distracteurs   : median={stats['distractor_words']['median']}, "
        f"mean={stats['distractor_words']['mean']}",
        "",
        "## Structure",
        "",
        f"- Distribution # options : {stats['n_options']}",
        f"- Distribution # bonnes réponses : {stats['n_correct_distrib']} *(briefing : exactement 1)*",
        f"- Target_version : {stats['target_versions']}",
        "",
        "## Patterns détectés",
        "",
        f"- Scénario-based (heuristique) : **{stats['scenario_count']}** ({100*stats['scenario_count']/n:.1f}%)",
        f"- Avec négation (not/never/except) : **{stats['negation_count']}** ({100*stats['negation_count']/n:.1f}%)",
        f"- 'All/None of the above' : **{stats['all_of_the_above_count']}** ({100*stats['all_of_the_above_count']/n:.1f}%)",
        "",
        "## Bilinguisme",
        "",
        f"- title_fr renseigné : **{stats['has_fr_title']}** / {n} ({100*stats['has_fr_title']/n:.1f}%)",
        f"- value_fr renseigné sur ≥ 1 option : {stats['has_fr_value']} (compté par option, pas par question)",
        f"- explication_claude renseignée : {stats['has_explication_claude']} / {n}",
        f"- question_image renseignée : {stats['has_image']} / {n}",
        "",
    ])
    if stats.get("inferred_modules"):
        lines.extend([
            "## Modules inférés (RAG sur title → top chunk)",
            "",
            "| Module | n |",
            "|---|---:|",
        ])
        for mod, count in sorted(stats["inferred_modules"].items(), key=lambda x: -x[1])[:25]:
            lines.append(f"| `{mod}` | {count} |")
        lines.extend(["", "**Distribution par tier :**", ""])
        for tier, count in sorted(stats["inferred_tier"].items(), key=lambda x: -x[1]):
            lines.append(f"- `{tier}` : {count}  ({100*count/n:.1f}%)")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrage style Udemy")
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_REPORT,
        help=f"Rapport markdown (défaut : {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--infer-modules", action="store_true",
        help="Inférer le module de chaque question via RAG (plus lent — embeddings).",
    )
    args = parser.parse_args()

    questions = load_udemy_questions()
    if not questions:
        print("❌ Aucune question Udemy trouvée.", file=sys.stderr)
        return 1

    stats = analyze(questions, infer_modules=args.infer_modules)
    md = render_markdown(stats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")
    print(md)
    print(f"\n→ Rapport écrit : {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
