#!/usr/bin/env python3
"""Classification automatique target_version (18.0 / 19.0 / both)."""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import ValidationError

from app.messages import build_classification_user_message
from app.odoo_docs_rag import search_doc_chunks
from app.prompts import CLASSIFICATION_SYSTEM_PROMPT, JSON_RETRY_USER_APPEND
from app.schemas import VersionClassification


def correct_answer_text(q: dict) -> str:
    """Texte de la bonne réponse validée en banque."""
    answers = q.get("answers") or []
    for a in answers:
        if not isinstance(a, dict):
            continue
        if a.get("is_correct"):
            return (a.get("value") or "").strip()
    return ""


def option_texts(q: dict) -> list[str]:
    out: list[str] = []
    for a in q.get("answers") or []:
        if isinstance(a, dict):
            v = (a.get("value") or "").strip()
            if v:
                out.append(v)
        elif a:
            out.append(str(a).strip())
    return out


def is_unclassified(q: dict) -> bool:
    tv = q.get("target_version")
    return tv is None or (isinstance(tv, str) and not str(tv).strip())


def fetch_classification_doc_chunks(question_text: str, options: list[str]) -> tuple[list[dict], list[dict]]:
    query = (question_text or "").strip()
    if options:
        query = f"{query}\n" + " ".join(options)
    from app.rag import doc_min_score

    min_sc = doc_min_score()
    v18 = search_doc_chunks(query, top_n=3, min_score=min_sc, version="18.0")
    v19 = search_doc_chunks(query, top_n=3, min_score=min_sc, version="19.0")
    return v18, v19


def classify_question(q: dict) -> tuple[VersionClassification, dict[str, Any]]:
    """
    Appelle Claude (prompt classification) et retourne (résultat, métadonnées).
    """
    from app.llm import (
        _answer_model,
        _anthropic_key,
        api_available,
        extract_text_from_content,
        strip_json_fences,
    )

    if not api_available():
        raise RuntimeError("Clé API Anthropic absente (config.json → anthropic.api_key).")

    title = (q.get("title") or "").strip()
    opts = option_texts(q)
    correct = correct_answer_text(q)
    if not title or not opts:
        raise ValueError(f"Question {q.get('id')} : titre ou options manquants.")
    if not correct:
        raise ValueError(f"Question {q.get('id')} : aucune bonne réponse marquée is_correct.")

    doc_v18, doc_v19 = fetch_classification_doc_chunks(title, opts)
    user_text = build_classification_user_message(
        title, opts, correct, doc_v18, doc_v19
    )

    import anthropic

    client = anthropic.Anthropic(api_key=_anthropic_key())
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
    t0 = time.perf_counter()
    response = client.messages.create(
        model=_answer_model(),
        max_tokens=800,
        system=CLASSIFICATION_SYSTEM_PROMPT,
        messages=messages,
    )
    text = extract_text_from_content(response.content)
    usage = getattr(response, "usage", None)
    meta: dict[str, Any] = {
        "model": getattr(response, "model", None) or _answer_model(),
        "latency_s": round(time.perf_counter() - t0, 3),
        "question_id": q.get("id"),
        "doc_v18_count": len(doc_v18),
        "doc_v19_count": len(doc_v19),
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
    }

    try:
        result = parse_classification(text)
        return result, meta
    except (ValueError, ValidationError, json.JSONDecodeError):
        retry_messages = messages + [
            {"role": "assistant", "content": text or "{}"},
            {"role": "user", "content": JSON_RETRY_USER_APPEND},
        ]
        response2 = client.messages.create(
            model=_answer_model(),
            max_tokens=800,
            system=CLASSIFICATION_SYSTEM_PROMPT,
            messages=retry_messages,
        )
        text2 = extract_text_from_content(response2.content)
        meta["retried_json"] = True
        meta["latency_s"] = round(time.perf_counter() - t0, 3)
        return parse_classification(text2), meta


def parse_classification(text: str) -> VersionClassification:
    from app.llm import strip_json_fences
    from quiz_llm import parse_json_value

    raw = strip_json_fences(text)
    data = parse_json_value(raw)
    if not isinstance(data, dict):
        raise ValueError("réponse JSON non objet")
    return VersionClassification.model_validate(data)
