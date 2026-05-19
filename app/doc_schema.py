#!/usr/bin/env python3
"""Schéma et migrations : chunks doc (SQLite) + target_version (questions.json)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

DOC_VERSIONS = frozenset({"18.0", "19.0"})
TARGET_VERSIONS = frozenset({"18.0", "19.0", "both"})
DEFAULT_DOC_VERSION = "18.0"
DEFAULT_TARGET_VERSION = "18.0"


def normalize_doc_version(version: str | None) -> str:
    v = (version or DEFAULT_DOC_VERSION).strip()
    if v not in DOC_VERSIONS:
        raise ValueError(f"version doc invalide : {v!r} (attendu : {sorted(DOC_VERSIONS)})")
    return v


def normalize_target_version(value: str | None) -> str:
    v = (value or DEFAULT_TARGET_VERSION).strip()
    if v not in TARGET_VERSIONS:
        raise ValueError(f"target_version invalide : {v!r} (attendu : {sorted(TARGET_VERSIONS)})")
    return v


def _chunks_has_column(conn: sqlite3.Connection, name: str) -> bool:
    rows = conn.execute("PRAGMA table_info(chunks)").fetchall()
    return any(r[1] == name for r in rows)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def migrate_docs_sqlite(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    Ajoute chunks.version + index (idempotent).
    Les lignes existantes reçoivent '18.0'.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pages (
            url TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            section TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    added_version = False
    if not _table_exists(conn, "chunks"):
        conn.executescript(
            """
            CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                section TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                version TEXT NOT NULL DEFAULT '18.0',
                FOREIGN KEY (url) REFERENCES pages(url) ON DELETE CASCADE
            );
            CREATE INDEX idx_chunks_url ON chunks(url);
            CREATE INDEX idx_chunks_version ON chunks(version);
            """
        )
    else:
        if not _chunks_has_column(conn, "version"):
            conn.execute(
                "ALTER TABLE chunks ADD COLUMN version TEXT NOT NULL DEFAULT '18.0'"
            )
            added_version = True
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_url ON chunks(url)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_version ON chunks(version)"
        )
    conn.execute(
        "UPDATE chunks SET version = '18.0' WHERE version IS NULL OR version = ''"
    )

    # Tables images (Phase 4 — pipeline images doc Odoo, scénario A).
    # `doc_images` : catalogue dédupliqué par hash MD5 du contenu image.
    # `chunk_images` : relation N-N entre chunks et images (par page).
    created_images_tables = not _table_exists(conn, "doc_images")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS doc_images (
            image_id    TEXT PRIMARY KEY,        -- MD5 du contenu image (hex)
            source_url  TEXT NOT NULL,           -- URL d'origine (peut varier pour un même contenu)
            local_path  TEXT NOT NULL,           -- chemin relatif à ROOT (static/doc_media/...)
            mime        TEXT NOT NULL DEFAULT 'image/webp',
            width       INTEGER,
            height      INTEGER,
            bytes       INTEGER,
            alt_text    TEXT NOT NULL DEFAULT '',
            caption     TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_doc_images_source ON doc_images(source_url);

        CREATE TABLE IF NOT EXISTS chunk_images (
            chunk_id  TEXT NOT NULL,
            image_id  TEXT NOT NULL,
            position  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chunk_id, image_id),
            FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE,
            FOREIGN KEY (image_id) REFERENCES doc_images(image_id)
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_images_chunk ON chunk_images(chunk_id);
        CREATE INDEX IF NOT EXISTS idx_chunk_images_image ON chunk_images(image_id);
        """
    )

    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    by_ver = dict(
        conn.execute(
            "SELECT version, COUNT(*) FROM chunks GROUP BY version ORDER BY version"
        ).fetchall()
    )
    images_total = conn.execute("SELECT COUNT(*) FROM doc_images").fetchone()[0]
    return {
        "added_version_column": added_version,
        "created_images_tables": created_images_tables,
        "chunks_total": int(total),
        "chunks_by_version": {str(k): int(v) for k, v in by_ver.items()},
        "doc_images_total": int(images_total),
    }


def migrate_questions_json(
    path: Path | None = None,
    *,
    write: bool = True,
) -> dict[str, Any]:
    """
    Ajoute target_version: '18.0' sur chaque question qui ne l'a pas.
    Crée un .bak horodaté avant écriture.
    """
    qpath = path or (ROOT / "questions.json")
    if not qpath.exists():
        raise FileNotFoundError(qpath)
    with open(qpath, encoding="utf-8") as f:
        data = json.load(f)
    questions = data.get("questions")
    if not isinstance(questions, list):
        raise ValueError("questions.json : clé 'questions' invalide")

    before_count = len(questions)
    updated = 0
    for q in questions:
        if not isinstance(q, dict):
            continue
        tv = q.get("target_version")
        if tv is None or (isinstance(tv, str) and not str(tv).strip()):
            q["target_version"] = DEFAULT_TARGET_VERSION
            updated += 1
        elif str(tv).strip().lower() == "null":
            q["target_version"] = None
        else:
            normalize_target_version(q.get("target_version"))

    after_count = len(questions)
    result = {
        "path": str(qpath),
        "questions_count": after_count,
        "target_version_added": updated,
        "written": False,
    }
    if write and updated > 0:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = qpath.with_suffix(f".json.bak.{stamp}")
        shutil.copy2(qpath, backup)
        with open(qpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        result["written"] = True
        result["backup"] = str(backup)
    elif write and updated == 0:
        result["written"] = False
        result["note"] = "déjà à jour"
    return result


def run_all_migrations(
    *,
    db_path: Path | None = None,
    questions_path: Path | None = None,
    write_questions: bool = True,
) -> dict[str, Any]:
    from app.odoo_docs_rag import db_path as default_db_path

    p = db_path or default_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        docs = migrate_docs_sqlite(conn)
    finally:
        conn.close()
    questions = migrate_questions_json(questions_path, write=write_questions)
    return {"sqlite": docs, "questions": questions}
