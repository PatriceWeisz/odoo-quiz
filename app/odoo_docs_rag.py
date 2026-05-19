#!/usr/bin/env python3
"""Recherche vectorielle sur chunks documentation Odoo 18 (SQLite)."""

from __future__ import annotations

import json
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent

_lock = threading.Lock()
_index_cache: dict[str, Any] | None = None
_index_mtime: float = 0.0


def _odoo_docs_cfg() -> dict[str, Any]:
    p = ROOT / "config.json"
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            od = json.load(f).get("odoo_docs")
            return od if isinstance(od, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def db_path() -> Path:
    rel = str(_odoo_docs_cfg().get("sqlite_path") or "data/odoo_docs.sqlite")
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def _blob_to_vec(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def _vec_to_blob(vec: np.ndarray) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec.astype(np.float32).tolist())


def store_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    url: str,
    title: str,
    section: str,
    text: str,
    embedding: np.ndarray,
    version: str = "18.0",
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO chunks (
            chunk_id, url, title, section, text, embedding, version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chunk_id, url, title, section, text, _vec_to_blob(embedding), version),
    )


def init_db(conn: sqlite3.Connection) -> None:
    from app.doc_schema import migrate_docs_sqlite

    migrate_docs_sqlite(conn)


def chunk_count(conn: sqlite3.Connection | None = None) -> int:
    p = db_path()
    if not p.exists():
        return 0
    own = conn is None
    if own:
        conn = sqlite3.connect(p)
    try:
        row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return int(row[0]) if row else 0
    finally:
        if own and conn is not None:
            conn.close()


def _load_index() -> dict[str, Any] | None:
    global _index_cache, _index_mtime
    p = db_path()
    if not p.exists():
        return None
    mtime = p.stat().st_mtime
    with _lock:
        if _index_cache is not None and mtime == _index_mtime:
            return _index_cache
        conn = sqlite3.connect(p)
        try:
            rows = conn.execute(
                "SELECT chunk_id, url, title, section, text, embedding, version FROM chunks"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        ids: list[str] = []
        meta: list[dict[str, str]] = []
        vecs: list[np.ndarray] = []
        for chunk_id, url, title, section, text, blob, version in rows:
            ids.append(chunk_id)
            meta.append(
                {
                    "chunk_id": chunk_id,
                    "url": url or "",
                    "title": title or "",
                    "section": section or "",
                    "text": text or "",
                    "version": (version or "18.0").strip() or "18.0",
                }
            )
            vecs.append(_blob_to_vec(blob))
        matrix = np.stack(vecs, axis=0).astype(np.float32, copy=False)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.maximum(norms, 1e-9)
        _index_cache = {"ids": ids, "meta": meta, "matrix": matrix}
        _index_mtime = mtime
        return _index_cache


def invalidate_doc_index_cache() -> None:
    global _index_cache, _index_mtime
    with _lock:
        _index_cache = None
        _index_mtime = 0.0


def _dedupe_chunks_by_section(chunks: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    """Garde le meilleur score par section/titre (mode target_version=both)."""
    ranked = sorted(chunks, key=lambda c: -float(c.get("score") or 0))
    seen: dict[str, dict[str, Any]] = {}
    for ch in ranked:
        key = (
            (ch.get("section") or ch.get("title") or ch.get("url") or "")
            .strip()
            .lower()
        )
        if not key:
            key = (ch.get("chunk_id") or "").strip()
        if key not in seen:
            seen[key] = ch
    return list(seen.values())[:top_n]


def search_doc_chunks(
    query: str,
    *,
    top_n: int = 5,
    min_score: float = 0.35,
    version: str | None = None,
) -> list[dict[str, Any]]:
    """Top chunks doc avec score de similarité cosinus (filtre version optionnel)."""
    from bank_embeddings import embed_query_text

    q = (query or "").strip()
    if not q:
        return []
    idx = _load_index()
    if not idx:
        return []
    qv = embed_query_text(q, timeout_s=None)
    if qv is None:
        return []
    norm = float(np.linalg.norm(qv))
    if norm < 1e-9:
        return []
    qv = qv / norm
    if not np.isfinite(qv).all():
        return []
    mat = idx["matrix"]
    if mat.shape[1] != qv.shape[0]:
        return []
    scores = (mat.astype(np.float64) @ qv.astype(np.float64)).astype(np.float32)
    if scores.size == 0:
        return []
    ver_filter = (version or "").strip() or None
    k = min(max(top_n * 4, top_n), scores.size)
    part = np.argpartition(scores, -k)[-k:]
    part = part[np.argsort(scores[part])[::-1]]
    out: list[dict[str, Any]] = []
    for i in part:
        sc = float(scores[i])
        if sc < min_score:
            continue
        row = dict(idx["meta"][i])
        if ver_filter and row.get("version") != ver_filter:
            continue
        row["score"] = round(sc, 4)
        out.append(row)
        if len(out) >= top_n:
            break
    return out
