#!/usr/bin/env python3
"""RAG hybride : questions banque + documentation Odoo (filtrée par version)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
_CFG_PATH = ROOT / "config.json"

_DOC_MIN_SCORE_DEFAULT = 0.35


def _load_odoo_docs_cfg() -> dict[str, Any]:
    if not _CFG_PATH.exists():
        return {}
    try:
        with open(_CFG_PATH, encoding="utf-8") as f:
            od = json.load(f).get("odoo_docs")
            return od if isinstance(od, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def doc_min_score() -> float:
    try:
        return float(_load_odoo_docs_cfg().get("min_similarity", _DOC_MIN_SCORE_DEFAULT))
    except (TypeError, ValueError):
        return _DOC_MIN_SCORE_DEFAULT


def _similar_qas(question_text: str, options: list[str] | None, question_bank: list[dict]) -> list[dict]:
    from bank_rag import filter_similar_for_prompt, find_similar_bank_questions

    query = (question_text or "").strip()
    if options:
        opt_part = " ".join(str(o).strip() for o in options if str(o).strip())
        if opt_part:
            query = f"{query}\n{opt_part}"
    if not query or not question_bank:
        return []
    similar = find_similar_bank_questions(query, question_bank, top_n=5)
    return filter_similar_for_prompt(similar)


def _doc_chunks(
    question_text: str,
    options: list[str] | None,
    target_version: str,
) -> list[dict]:
    from app.odoo_docs_rag import _dedupe_chunks_by_section, search_doc_chunks

    query = (question_text or "").strip()
    if options:
        query = f"{query}\n" + " ".join(str(o).strip() for o in options if str(o).strip())
    min_sc = doc_min_score()
    tv = (target_version or "18.0").strip()

    if tv == "both":
        v18 = search_doc_chunks(query, top_n=5, min_score=min_sc, version="18.0")
        v19 = search_doc_chunks(query, top_n=5, min_score=min_sc, version="19.0")
        return _dedupe_chunks_by_section(v18 + v19, top_n=5)

    return search_doc_chunks(query, top_n=5, min_score=min_sc, version=tv)


def build_context(
    question_text: str,
    options: list[str] | None,
    *,
    question_bank: list[dict] | None = None,
    target_version: str | None = None,
) -> dict[str, Any]:
    """
    Retourne {similar_qas, doc_chunks, target_version} prêt pour build_user_message.
    target_version : certification cible (18.0 / 19.0) ou 'both' pour une question taguée both.
    """
    from app.config import get_target_certification

    tv = (target_version or get_target_certification()).strip()
    bank = question_bank or []
    return {
        "similar_qas": _similar_qas(question_text, options, bank),
        "doc_chunks": _doc_chunks(question_text, options, tv),
        "target_version": tv,
    }
