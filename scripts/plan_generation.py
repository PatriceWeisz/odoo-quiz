#!/usr/bin/env python3
"""Plan de génération — Phase 5.3 du briefing.

Calcule combien de questions générer par (tier, module, version) pour
atteindre un budget cible donné, en respectant :

  - 70 % du budget pour `cert`, 20 % pour `tier1`, 10 % pour `tier2`
    (paramétrable via TIER_BUDGET dans app.study_modules)
  - 40 % v18, 60 % v19, sauf pour les modules v19-only à 100 % v19
  - PLAFONNEMENT : pour ne pas générer plus que ce que la doc permet,
    target_per_(module, version) = min(planned_quota, n_chunks × factor)
    avec factor=2 pour modules à faible volume (<100 chunks/v) et
    factor=3 pour modules denses (≥100 chunks/v).

Sortie :
  - data/generation_plan.json     : structuré, machine-readable (pipeline 5.5)
  - data/generation_plan_report.md : lisible humain

Usage :
  python3 -m scripts.plan_generation                  # 3000 questions
  python3 -m scripts.plan_generation --total 1000     # plus petit budget
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.odoo_docs_rag import db_path  # noqa: E402
from app.study_modules import (  # noqa: E402
    ALL_TIERS,
    STUDY_MODULES,
    TIER_BUDGET,
    is_v19_only,
    url_paths_for,
)

DEFAULT_TOTAL = 3000
V18_RATIO = 0.40
V19_RATIO = 0.60
FACTOR_LIGHT_DOC = 2     # pour modules < 100 chunks/version
FACTOR_DENSE_DOC = 3     # pour modules ≥ 100 chunks/version
DENSE_THRESHOLD = 100


# --- Comptage chunks par (module, version) -----------------------------------


def _count_chunks_for_module(
    conn: sqlite3.Connection, version: str, module: str
) -> int:
    patterns: list[str] = []
    for path in url_paths_for(module):
        patterns.append(f"%/applications/{path}/%")
        patterns.append(f"%/applications/{path}.html%")
    sql = (
        "SELECT COUNT(*) FROM chunks WHERE version = ? AND ("
        + " OR ".join("url LIKE ?" for _ in patterns)
        + ")"
    )
    row = conn.execute(sql, (version, *patterns)).fetchone()
    return int(row[0]) if row else 0


def collect_chunk_counts() -> dict[str, dict[str, int]]:
    """Retourne {module: {version: n_chunks, ...}, ...}."""
    p = db_path()
    if not p.exists():
        raise SystemExit(f"❌ DB doc Odoo introuvable : {p}")
    conn = sqlite3.connect(p)
    counts: dict[str, dict[str, int]] = {}
    try:
        for tier in ALL_TIERS:
            for module in STUDY_MODULES[tier]:
                counts[module] = {
                    "18.0": _count_chunks_for_module(conn, "18.0", module),
                    "19.0": _count_chunks_for_module(conn, "19.0", module),
                }
    finally:
        conn.close()
    return counts


# --- Plan ---------------------------------------------------------------------


def _cap_factor(n_chunks: int) -> int:
    return FACTOR_DENSE_DOC if n_chunks >= DENSE_THRESHOLD else FACTOR_LIGHT_DOC


def build_plan(total: int, chunk_counts: dict[str, dict[str, int]]) -> dict:
    """Construit le plan de génération.

    Étapes :
      1. Répartit `total` entre tiers selon TIER_BUDGET (70/20/10).
      2. Pour chaque tier, répartit le budget tier entre ses modules
         proportionnellement à la masse documentaire (chunks v18 + v19).
         Évite de fixer un même quota à des modules de volume très inégaux.
      3. Pour chaque module, répartit entre v18/v19 :
         - V19_ONLY → 100 % v19
         - Sinon → 40 % v18, 60 % v19
      4. PLAFONNEMENT : si quota > n_chunks × factor, écrête. Le surplus est
         redistribué sur le même module dans l'autre version si possible.
         Ce qui ne peut pas être absorbé est compté dans `overflow`.
    """
    plan: dict = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total_target": total,
            "tier_budget": dict(TIER_BUDGET),
            "version_ratios": {"18.0": V18_RATIO, "19.0": V19_RATIO},
            "cap_factor_light": FACTOR_LIGHT_DOC,
            "cap_factor_dense": FACTOR_DENSE_DOC,
            "dense_threshold_chunks": DENSE_THRESHOLD,
        },
        "tiers": {},
    }

    overflow_per_tier: dict[str, int] = {t: 0 for t in ALL_TIERS}

    for tier in ALL_TIERS:
        tier_budget = round(total * TIER_BUDGET[tier])
        modules = STUDY_MODULES[tier]

        masses: dict[str, int] = {
            m: chunk_counts[m]["18.0"] + chunk_counts[m]["19.0"]
            for m in modules
        }
        total_mass = sum(masses.values()) or 1

        tier_modules: list[dict] = []
        for m in modules:
            share = masses[m] / total_mass
            module_quota = int(round(tier_budget * share))

            if is_v19_only(m):
                q18, q19 = 0, module_quota
            else:
                q18 = int(round(module_quota * V18_RATIO))
                q19 = module_quota - q18

            cap18 = chunk_counts[m]["18.0"] * _cap_factor(chunk_counts[m]["18.0"])
            cap19 = chunk_counts[m]["19.0"] * _cap_factor(chunk_counts[m]["19.0"])

            # Réallocation interne v18↔v19 si plafond dépassé
            if q18 > cap18:
                over = q18 - cap18
                q18 = cap18
                room19 = max(0, cap19 - q19)
                add = min(over, room19)
                q19 += add
                over -= add
                overflow_per_tier[tier] += over
            if q19 > cap19:
                over = q19 - cap19
                q19 = cap19
                room18 = max(0, cap18 - q18) if not is_v19_only(m) else 0
                add = min(over, room18)
                q18 += add
                over -= add
                overflow_per_tier[tier] += over

            tier_modules.append({
                "module": m,
                "v19_only": is_v19_only(m),
                "chunks_v18": chunk_counts[m]["18.0"],
                "chunks_v19": chunk_counts[m]["19.0"],
                "cap_v18": cap18,
                "cap_v19": cap19,
                "target_v18": q18,
                "target_v19": q19,
                "target_total": q18 + q19,
            })

        plan["tiers"][tier] = {
            "budget": tier_budget,
            "modules": tier_modules,
            "computed_total": sum(m["target_total"] for m in tier_modules),
            "overflow": overflow_per_tier[tier],
        }

    plan["meta"]["grand_total"] = sum(
        t["computed_total"] for t in plan["tiers"].values()
    )
    plan["meta"]["total_overflow"] = sum(overflow_per_tier.values())
    return plan


# --- Rendu markdown -----------------------------------------------------------


def render_markdown(plan: dict) -> str:
    m = plan["meta"]
    lines = [
        "# Plan de génération — Phase 5",
        "",
        f"*Généré le {m['generated_at']}*",
        "",
        f"- **Cible totale** : {m['total_target']} questions",
        f"- Tier budget : {m['tier_budget']}",
        f"- Ratios versions : v18={m['version_ratios']['18.0']}, v19={m['version_ratios']['19.0']}",
        f"- Plafond : `target ≤ n_chunks × {m['cap_factor_light']}` (modules <{DENSE_THRESHOLD} chunks) "
        f"ou `× {m['cap_factor_dense']}` (≥{DENSE_THRESHOLD}, denses)",
        "",
        f"**Total atteignable** : **{m['grand_total']}** questions",
        f"**Overflow (non-allouable par plafonnement)** : {m['total_overflow']}",
        "",
    ]
    for tier in ALL_TIERS:
        t = plan["tiers"][tier]
        lines.extend([
            f"## Tier `{tier}` — budget {t['budget']}, atteignable **{t['computed_total']}**, overflow {t['overflow']}",
            "",
            "| Module | chunks v18 | chunks v19 | cap v18 | cap v19 | target v18 | target v19 | total |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for mod in t["modules"]:
            v19o = " 🆕" if mod["v19_only"] else ""
            lines.append(
                f"| `{mod['module']}`{v19o} | "
                f"{mod['chunks_v18']} | {mod['chunks_v19']} | "
                f"{mod['cap_v18']} | {mod['cap_v19']} | "
                f"**{mod['target_v18']}** | **{mod['target_v19']}** | "
                f"**{mod['target_total']}** |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


# --- Main ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan de génération de questions")
    parser.add_argument(
        "--total", type=int, default=DEFAULT_TOTAL,
        help=f"Budget total de questions à générer (défaut : {DEFAULT_TOTAL})",
    )
    parser.add_argument(
        "--output-json", type=Path,
        default=ROOT / "data" / "generation_plan.json",
        help="Plan JSON (défaut : data/generation_plan.json)",
    )
    parser.add_argument(
        "--output-md", type=Path,
        default=ROOT / "data" / "generation_plan_report.md",
        help="Rapport markdown (défaut : data/generation_plan_report.md)",
    )
    args = parser.parse_args()

    chunk_counts = collect_chunk_counts()
    plan = build_plan(args.total, chunk_counts)

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    args.output_md.write_text(render_markdown(plan), encoding="utf-8")

    print(render_markdown(plan))
    print(f"→ JSON : {args.output_json}")
    print(f"→ MD   : {args.output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
