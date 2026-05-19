#!/usr/bin/env python3
"""Extraction structurée depuis le DOM (favori pleine page) — Vision en secours."""

from __future__ import annotations

from typing import Any

from import_udemy import title_requires_capture_image, validate_udemy_item


def coerce_dom_payload(parsed: Any) -> list[dict]:
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    if isinstance(parsed, dict):
        for key in ("items", "questions", "quiz_items"):
            inner = parsed.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        return [parsed]
    return []


def _normalize_dom_item(raw: dict, cap_src: str) -> dict:
    title = (raw.get("title") or "").strip()
    answers = raw.get("answers")
    if not isinstance(answers, list):
        answers = []
    answers = [str(a).strip() for a in answers if str(a).strip()]
    need_img = bool(raw.get("needs_question_image"))
    if not need_img and title:
        need_img = title_requires_capture_image(title)
    ci_vis = bool(raw.get("correct_index_visible"))
    ci = raw.get("correct_index")
    if not ci_vis:
        ci = None
    return {
        "title": title,
        "answers": answers,
        "correct_index": ci,
        "correct_index_visible": ci_vis,
        "explication_udemy": (raw.get("explication_udemy") or "").strip(),
        "needs_question_image": need_img,
        "crop_rel": raw.get("crop_rel"),
        "_capture_source": cap_src,
        "_extract_method": "dom",
    }


def items_from_dom_payload(payload: Any, capture_source: str = "udemy") -> list[dict]:
    from import_screenshot import MAX_QUESTIONS_PER_CAPTURE

    cap_src = (capture_source or "udemy").strip().lower()
    cap_src = (
        "odoo"
        if cap_src in ("odoo", "odoo_web", "website", "elearning", "slides")
        else "udemy"
    )
    raw_items = coerce_dom_payload(payload)
    if not raw_items:
        raise ValueError("Payload DOM vide.")

    out: list[dict] = []
    for i, raw in enumerate(raw_items):
        draft = _normalize_dom_item(raw, cap_src)
        if not draft["title"] and len(draft["answers"]) < 2:
            continue
        out.append(validate_udemy_item(draft, i))

    if not out:
        raise ValueError("Aucune question valide dans le DOM.")
    if len(out) > MAX_QUESTIONS_PER_CAPTURE:
        out = out[:MAX_QUESTIONS_PER_CAPTURE]
    for it in out:
        it["_extract_method"] = "dom"
    return out


def dom_items_need_vision_fallback(items: list[dict]) -> tuple[bool, str]:
    """True si la capture image + Vision est nécessaire."""
    if not items:
        return True, "aucune question extraite du DOM"
    for it in items:
        title = (it.get("title") or "").strip()
        answers = it.get("answers")
        if not title:
            return True, "énoncé vide dans le DOM"
        if not isinstance(answers, list) or len(answers) < 2:
            return True, "options insuffisantes dans le DOM"
        if it.get("needs_question_image"):
            return True, "la question repose sur une image ou une UI visible"
        if title_requires_capture_image(title):
            return True, "l'énoncé renvoie à une image ou un écran"
    return False, ""
