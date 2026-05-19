#!/usr/bin/env python3
"""
Audit de couverture documentaire Odoo (chunks SQLite) pour certification v18/v19.

python3 -m scripts.audit_doc_coverage
python3 -m scripts.audit_doc_coverage --output data/doc_coverage_report.md
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_REPORT = ROOT / "data" / "doc_coverage_report.md"

MIN_TOTAL_CHUNKS = 6000
MIN_MODULE_CHUNKS = 150

# Source de vérité — périmètre des modules à étudier (3 tiers).
from app.study_modules import (  # noqa: E402
    MODULE_URL_PATHS,
    STUDY_MODULES,
    V19_ONLY_MODULES,
    all_modules,
    tier_of,
)


@dataclass
class ModuleRow:
    module: str
    tier: str
    v18: int
    v19: int
    status: str


def _db_path() -> Path:
    from app.odoo_docs_rag import db_path

    return db_path()


def _paths_for_module(module: str) -> list[str]:
    return MODULE_URL_PATHS.get(module, [module])


def _count_chunks(conn: sqlite3.Connection, version: str, module: str) -> int:
    """Compte les chunks dont l'URL couvre le chemin module sous /applications/."""
    patterns: list[str] = []
    for path in _paths_for_module(module):
        patterns.append(f"%/applications/{path}/%")
        patterns.append(f"%/applications/{path}.html%")
    sql = (
        "SELECT COUNT(*) FROM chunks WHERE version = ? AND ("
        + " OR ".join("url LIKE ?" for _ in patterns)
        + ")"
    )
    row = conn.execute(sql, (version, *patterns)).fetchone()
    return int(row[0]) if row else 0


def _count_null_embeddings(conn: sqlite3.Connection, version: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE version = ? AND (embedding IS NULL OR length(embedding) = 0)",
        (version,),
    ).fetchone()
    return int(row[0]) if row else 0


def _status_for_module(module: str, v18: int, v19: int) -> str:
    if module in V19_ONLY_MODULES:
        parts = []
        if v18 > 0:
            parts.append(f"v18 inattendu ({v18})")
        if v19 < MIN_MODULE_CHUNKS:
            parts.append(f"v19 sous-représenté ({v19} < {MIN_MODULE_CHUNKS})")
        elif v19 == 0:
            parts.append("v19 manquant")
        if not parts:
            return "✅ v19-only"
        return "❌ " + "; ".join(parts)

    issues = []
    if v18 < MIN_MODULE_CHUNKS:
        issues.append(f"v18 sous-représenté ({v18})")
    if v19 < MIN_MODULE_CHUNKS:
        issues.append(f"v19 sous-représenté ({v19})")
    if v18 == 0 and v19 == 0:
        return "❌ absent v18 et v19"
    if issues:
        return "⚠️ " + "; ".join(issues)
    return "✅"


def run_audit(conn: sqlite3.Connection) -> tuple[dict[str, int], list[ModuleRow], dict[str, int]]:
    totals: dict[str, int] = {}
    null_emb: dict[str, int] = {}
    for ver in ("18.0", "19.0"):
        row = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE version = ?", (ver,)
        ).fetchone()
        totals[ver] = int(row[0]) if row else 0
        null_emb[ver] = _count_null_embeddings(conn, ver)

    modules: list[ModuleRow] = []
    for mod in all_modules():
        v18 = _count_chunks(conn, "18.0", mod)
        v19 = _count_chunks(conn, "19.0", mod)
        modules.append(
            ModuleRow(
                module=mod,
                tier=tier_of(mod) or "?",
                v18=v18,
                v19=v19,
                status=_status_for_module(mod, v18, v19),
            )
        )
    return totals, modules, null_emb


def _total_ok(n: int) -> str:
    return "✅" if n >= MIN_TOTAL_CHUNKS else "❌"


def _build_markdown(
    totals: dict[str, int],
    modules: list[ModuleRow],
    null_emb: dict[str, int],
    *,
    db_path: Path,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    v18_total = totals.get("18.0", 0)
    v19_total = totals.get("19.0", 0)

    v18_modules_low = [m for m in modules if m.module not in V19_ONLY_MODULES and m.v18 < MIN_MODULE_CHUNKS]
    v19_modules_low = [m for m in modules if m.v19 < MIN_MODULE_CHUNKS]
    v18_only_issues = [m for m in modules if m.module in V19_ONLY_MODULES and m.v18 > 0]

    need_v18 = v18_total < MIN_TOTAL_CHUNKS or bool(v18_modules_low)
    need_v19 = v19_total < MIN_TOTAL_CHUNKS or bool(v19_modules_low)
    coverage_ok = not need_v18 and not need_v19

    lines = [
        "# Audit de couverture documentaire Odoo",
        "",
        f"*Généré le {now} — base `{db_path}`*",
        "",
        "## Totaux",
        "",
        f"- Odoo 18.0 : **{v18_total}** chunks {_total_ok(v18_total)} (cible ≥ {MIN_TOTAL_CHUNKS})",
        f"- Odoo 19.0 : **{v19_total}** chunks {_total_ok(v19_total)} (cible ≥ {MIN_TOTAL_CHUNKS})",
        "",
        "### Embeddings",
        "",
        f"- v18 sans embedding : **{null_emb.get('18.0', 0)}**",
        f"- v19 sans embedding : **{null_emb.get('19.0', 0)}**",
        "",
        "## Détail par module",
        "",
        f"Seuil par module : **≥ {MIN_MODULE_CHUNKS}** chunks (sauf modules v19-only : 0 attendu en v18).",
        "",
        "| Tier | Module | v18 chunks | v19 chunks | Statut |",
        "|---|---|---:|---:|---|",
    ]

    for m in modules:
        v18_disp = "—" if m.module in V19_ONLY_MODULES and m.v18 == 0 else str(m.v18)
        lines.append(f"| `{m.tier}` | `{m.module}` | {v18_disp} | {m.v19} | {m.status} |")

    lines.extend(["", "## Recommandation", ""])

    if coverage_ok:
        lines.append("- [x] Couverture OK, passer à la génération de questions")
        lines.append("- [ ] Re-ingestion v18 requise")
        lines.append("- [ ] Re-ingestion v19 requise")
    else:
        lines.append("- [ ] Couverture OK, passer à la génération")
        if need_v18:
            mods = ", ".join(m.module for m in v18_modules_low) or "total insuffisant"
            lines.append(f"- [ ] **Re-ingestion v18 requise** ({mods})")
        else:
            lines.append("- [x] Re-ingestion v18 non nécessaire")
        if need_v19:
            mods = ", ".join(m.module for m in v19_modules_low) or "total insuffisant"
            lines.append(f"- [ ] **Re-ingestion v19 requise** ({mods})")
        else:
            lines.append("- [x] Re-ingestion v19 non nécessaire")

    if v18_only_issues:
        lines.append("")
        lines.append(
            f"*Note : modules v19-only avec chunks v18 inattendus : "
            f"{', '.join(m.module for m in v18_only_issues)}*"
        )

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit couverture doc Odoo v18/v19")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"Rapport markdown (défaut : {DEFAULT_REPORT})",
    )
    args = parser.parse_args()

    db = _db_path()
    if not db.exists():
        print(f"❌ Base introuvable : {db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db)
    try:
        totals, modules, null_emb = run_audit(conn)
    finally:
        conn.close()

    report = _build_markdown(totals, modules, null_emb, db_path=db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")

    print(report)
    print(f"\n→ Rapport écrit : {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
