#!/usr/bin/env python3
"""Migration schéma v18/v19 : chunks.version + questions.target_version."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration schéma odoo-quiz (étape 1)")
    parser.add_argument(
        "--questions-only",
        action="store_true",
        help="Ne migrer que questions.json",
    )
    parser.add_argument(
        "--sqlite-only",
        action="store_true",
        help="Ne migrer que odoo_docs.sqlite",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="questions.json : compter sans écrire",
    )
    args = parser.parse_args()

    from app.doc_schema import migrate_docs_sqlite, migrate_questions_json
    from app.odoo_docs_rag import db_path, invalidate_doc_index_cache

    out: dict = {}

    if not args.questions_only:
        p = db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3

        conn = sqlite3.connect(p)
        try:
            out["sqlite"] = migrate_docs_sqlite(conn)
        finally:
            conn.close()
        invalidate_doc_index_cache()
        print(f"SQLite {p}:")
        print(json.dumps(out["sqlite"], indent=2, ensure_ascii=False))

    if not args.sqlite_only:
        q = migrate_questions_json(write=not args.dry_run)
        out["questions"] = q
        print(f"\nquestions.json:")
        print(json.dumps(q, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
