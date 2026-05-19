#!/usr/bin/env python3
"""
Ajuste generation_plan.json : plafond réaliste par module (2 questions max / chunk doc).

python3 -m scripts.plan_generation
python3 -m scripts.plan_generation --write
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLAN_PATH = ROOT / "data" / "generation_plan.json"

DEFAULT_PLANNED_QUOTA = 80


def _chunk_count(conn: sqlite3.Connection, module: str, version: str) -> int:
    from scripts.audit_doc_coverage import MODULE_URL_PATHS

    patterns: list[str] = []
    for path in MODULE_URL_PATHS.get(module, [module]):
        patterns.append(f"%/applications/{path}/%")
        patterns.append(f"%/applications/{path}.html%")
    sql = (
        "SELECT COUNT(*) FROM chunks WHERE version = ? AND ("
        + " OR ".join("url LIKE ?" for _ in patterns)
        + ")"
    )
    row = conn.execute(sql, (version, *patterns)).fetchone()
    return int(row[0]) if row else 0


def apply_quota_rule(planned: int, n_chunks: int) -> int:
    """target_questions = min(planned_quota, n_chunks * 2)."""
    if n_chunks <= 0:
        return 0
    return min(planned, n_chunks * 2)


def main() -> int:
    from scripts.audit_doc_coverage import CERT_FUNCTIONAL_MODULES
    from app.odoo_docs_rag import db_path

    parser = argparse.ArgumentParser(description="Plan de génération avec quotas réalistes")
    parser.add_argument("--write", action="store_true", help="Écrire data/generation_plan.json")
    parser.add_argument("--cert", default="19.0", choices=("18.0", "19.0"))
    args = parser.parse_args()

    db = db_path()
    if not db.exists():
        print(f"❌ Base introuvable : {db}", file=sys.stderr)
        return 1

    if PLAN_PATH.exists():
        plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    else:
        plan = {"target_certification": args.cert, "modules": {}}

    conn = sqlite3.connect(db)
    try:
        rows: list[dict] = []
        for mod in CERT_FUNCTIONAL_MODULES:
            n = _chunk_count(conn, mod, args.cert)
            planned = int((plan.get("modules") or {}).get(mod, {}).get("planned_quota", DEFAULT_PLANNED_QUOTA))
            target = apply_quota_rule(planned, n)
            rows.append(
                {
                    "module": mod,
                    "n_chunks": n,
                    "planned_quota": planned,
                    "target_questions": target,
                }
            )
            if "modules" not in plan:
                plan["modules"] = {}
            plan["modules"][mod] = {
                "planned_quota": planned,
                "target_questions": target,
                "n_chunks": n,
            }
    finally:
        conn.close()

    print(f"Certification cible : {args.cert}\n")
    print(f"{'Module':<35} {'chunks':>6} {'plan':>6} {'cible':>6}")
    print("-" * 58)
    for r in rows:
        print(f"{r['module']:<35} {r['n_chunks']:>6} {r['planned_quota']:>6} {r['target_questions']:>6}")

    if args.write:
        PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan["target_certification"] = args.cert
        PLAN_PATH.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\n→ Écrit : {PLAN_PATH}")
    else:
        print(f"\n(dry-run — ajoutez --write pour enregistrer {PLAN_PATH})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
