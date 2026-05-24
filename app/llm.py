#!/usr/bin/env python3
"""Appels Anthropic pour suggestions de réponses (JSON structuré)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.messages import build_user_message
from app.prompts import JSON_RETRY_USER_APPEND, format_system_prompt
from app.schemas import AnswerSuggestion

ROOT = Path(__file__).resolve().parent.parent

_LETTER_RE = re.compile(r"[A-Z]")


def _cfg() -> dict:
    p = ROOT / "config.json"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _answer_model() -> str:
    a = _cfg().get("anthropic") or {}
    answer = (a.get("answer_model") or "").strip()
    if answer:
        return answer
    vision = (a.get("vision_model") or "claude-sonnet-4-6").strip()
    text = (a.get("text_model") or "claude-haiku-4-5").strip()
    if text == "claude-haiku-4-5":
        return vision
    return text


def escalation_model() -> str:
    """Modèle d'escalade (plus puissant) pour les cas de confiance non-haute.

    Lu dans config.json → anthropic.escalation_model. Défaut : Opus 4.6.
    Mettre "" (chaîne vide) pour désactiver l'escalade.
    """
    a = _cfg().get("anthropic") or {}
    if "escalation_model" in a:
        return (a.get("escalation_model") or "").strip()
    return "claude-opus-4-6"


def api_available() -> bool:
    return bool(((_cfg().get("anthropic") or {}).get("api_key") or "").strip())


def _anthropic_key() -> str:
    return ((_cfg().get("anthropic") or {}).get("api_key") or "").strip()


def extract_text_from_content(content: Any) -> str:
    """Concatène les blocs type=text d'une réponse Messages API."""
    parts: list[str] = []
    for block in content or []:
        btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if btype != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text))
    return "".join(parts).strip()


def strip_json_fences(text: str) -> str:
    s = (text or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", s, re.I)
    if m:
        return m.group(1).strip()
    return s


def _doc_chunks_relevant(question_text: str, chunks: list[dict[str, Any]]) -> bool:
    if not chunks:
        return False
    ql = (question_text or "").lower()
    technical_ids = re.findall(r"\b(?:model\.)?x_[\w]+\b", ql) + re.findall(r"\bmodel\.\w+\b", ql)
    if technical_ids:
        for ch in chunks:
            text = (ch.get("text") or "").lower()
            if any(tid in text for tid in technical_ids):
                return True
        return False
    keys = [w for w in re.findall(r"[\w.]{8,}", ql)]
    if not keys:
        keys = [w for w in re.findall(r"\w{5,}", ql)]
    for ch in chunks:
        text = (ch.get("text") or "").lower()
        if keys and any(k in text for k in keys):
            return True
    return any(float(ch.get("score") or 0) >= 0.5 for ch in chunks)


def apply_confidence_guard(
    suggestion: AnswerSuggestion,
    context: dict[str, Any] | None,
    *,
    question_text: str = "",
) -> AnswerSuggestion:
    """Abaisse la confiance si aucune source doc / banque ne soutient une affirmation technique."""
    ctx = context or {}
    doc_chunks = ctx.get("doc_chunks") or []
    has_doc_ctx = _doc_chunks_relevant(question_text, doc_chunks)
    has_sim_ctx = bool(ctx.get("similar_qas"))
    src_types = {s.type for s in suggestion.sources}
    has_doc_ref = "doc_chunk" in src_types
    has_sim_ref = "similar_qa" in src_types
    if suggestion.confiance == "haute" and not has_doc_ref and not has_sim_ref:
        suggestion = suggestion.model_copy(update={"confiance": "moyenne"})
    # Aucun extrait doc ni Q/R banque injectés → plafond basse (web_search seul insuffisant).
    if not has_doc_ctx and not has_sim_ctx:
        suggestion = suggestion.model_copy(update={"confiance": "basse"})
    return suggestion


def parse_answer_suggestion(text: str) -> AnswerSuggestion:
    from quiz_llm import parse_json_value

    raw = strip_json_fences(text)
    data = parse_json_value(raw)
    if not isinstance(data, dict):
        raise ValueError("réponse JSON non objet")
    return AnswerSuggestion.model_validate(data)


def reponse_to_correct_index(reponse: str, n_options: int) -> int | None:
    """Convertit 'B' ou 'A,C' en index 1-based (première lettre si plusieurs)."""
    letters = _LETTER_RE.findall((reponse or "").upper())
    if not letters:
        return None
    idx = ord(letters[0]) - ord("A") + 1
    if 1 <= idx <= n_options:
        return idx
    return None


def _build_user_content(
    user_text: str,
    image_paths: list[str] | None,
) -> str | list[dict[str, Any]]:
    paths = [p for p in (image_paths or []) if p]
    if not paths:
        return user_text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for path in paths:
        p = Path(path)
        if not p.is_file():
            continue
        raw = p.read_bytes()
        media = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        import base64

        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media,
                    "data": base64.standard_b64encode(raw).decode("ascii"),
                },
            }
        )
    return blocks


def _resolve_target_versions(
    context: dict[str, Any] | None,
    target_version: str | None,
) -> tuple[str, str]:
    from app.config import get_target_certification, other_cert_version

    tv = (target_version or (context or {}).get("target_version") or get_target_certification()).strip()
    if tv == "both":
        tv = get_target_certification()
    return tv, other_cert_version(tv)


def _create_messages(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 1500,
    model: str | None = None,
    target_version: str | None = None,
    other_version: str | None = None,
) -> Any:
    import anthropic

    client = anthropic.Anthropic(api_key=_anthropic_key())
    tv = target_version or "18.0"
    ov = other_version or ("19.0" if tv == "18.0" else "18.0")
    kwargs: dict[str, Any] = {
        "model": model or _answer_model(),
        "max_tokens": max_tokens,
        "system": format_system_prompt(tv, ov),
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
    return client.messages.create(**kwargs)


def suggest_answer(
    question_text: str,
    options: list[str] | None,
    context: dict[str, Any] | None,
    *,
    image_paths: list[str] | None = None,
    question_id: int | str | None = None,
    use_web_tools: bool = False,
    target_version: str | None = None,
    model: str | None = None,
) -> tuple[AnswerSuggestion, dict[str, Any]]:
    """
    Appelle Claude et retourne (AnswerSuggestion, métadonnées d'appel).
    use_web_tools : web_search / web_fetch restreints odoo.com.
    model : override de modèle (ex. escalade vers Opus) ; défaut = answer_model.
    """
    if not api_available():
        raise RuntimeError("Clé API Anthropic absente (config.json → anthropic.api_key).")

    tv, ov = _resolve_target_versions(context, target_version)
    user_text = build_user_message(question_text, options, context)
    user_content = _build_user_content(user_text, image_paths)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]

    tools: list[dict[str, Any]] | None = None
    if use_web_tools:
        tools = _web_tools()

    t0 = time.perf_counter()
    response = _create_messages(
        messages=messages, tools=tools, target_version=tv, other_version=ov, model=model
    )
    latency_s = time.perf_counter() - t0

    text = extract_text_from_content(response.content)
    usage = getattr(response, "usage", None)
    meta: dict[str, Any] = {
        "model": getattr(response, "model", None) or _answer_model(),
        "latency_s": round(latency_s, 3),
        "question_id": question_id,
        "target_version": tv,
        "other_version": ov,
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
        "retried_json": False,
    }

    try:
        suggestion = apply_confidence_guard(
            parse_answer_suggestion(text), context, question_text=question_text
        )
        _log_call(suggestion, meta)
        return suggestion, meta
    except (ValueError, ValidationError, json.JSONDecodeError) as first_err:
        meta["retried_json"] = True
        retry_messages = messages + [
            {"role": "assistant", "content": text or "{}"},
            {"role": "user", "content": JSON_RETRY_USER_APPEND},
        ]
        response2 = _create_messages(
            messages=retry_messages, tools=tools, target_version=tv, other_version=ov, model=model
        )
        meta["latency_s"] = round(time.perf_counter() - t0, 3)
        usage2 = getattr(response2, "usage", None)
        if usage2:
            meta["input_tokens"] = (meta.get("input_tokens") or 0) + getattr(
                usage2, "input_tokens", 0
            )
            meta["output_tokens"] = (meta.get("output_tokens") or 0) + getattr(
                usage2, "output_tokens", 0
            )
        text2 = extract_text_from_content(response2.content)
        try:
            suggestion = apply_confidence_guard(
                parse_answer_suggestion(text2), context, question_text=question_text
            )
            _log_call(suggestion, meta)
            return suggestion, meta
        except Exception as e2:
            raise ValueError(f"JSON invalide après retry : {e2}") from first_err


def _log_call(suggestion: AnswerSuggestion, meta: dict[str, Any]) -> None:
    try:
        from app.telemetry import log_suggestion

        log_suggestion(
            question_id=meta.get("question_id"),
            model=meta.get("model"),
            confiance=suggestion.confiance,
            sources=[s.model_dump() for s in suggestion.sources],
            latency_s=meta.get("latency_s"),
            input_tokens=meta.get("input_tokens"),
            output_tokens=meta.get("output_tokens"),
            target_version=meta.get("target_version"),
            extra={
                "retried_json": meta.get("retried_json"),
                "alerte_version": suggestion.alerte_version,
                "other_version": meta.get("other_version"),
            },
        )
    except Exception:
        pass


def _web_tools() -> list[dict[str, Any]]:
    """Outils web restreints odoo.com (étape 2)."""
    return [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": 3,
            "allowed_domains": ["odoo.com", "www.odoo.com"],
        },
        {
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": 3,
            "allowed_domains": ["odoo.com", "www.odoo.com"],
        },
    ]


def suggest_answer_with_web_tools(
    question_text: str,
    options: list[str] | None,
    context: dict[str, Any] | None,
    **kwargs: Any,
) -> tuple[AnswerSuggestion, dict[str, Any]]:
    """Alias explicite avec outils web (retombe si version outil non supportée)."""
    try:
        return suggest_answer(
            question_text, options, context, use_web_tools=True, **kwargs
        )
    except Exception as e:
        err = str(e).lower()
        if "web_search_20260209" in err or "web_fetch_20260209" in err or "not found" in err:
            return _suggest_with_legacy_web_tools(question_text, options, context, **kwargs)
        raise


def _suggest_with_legacy_web_tools(
    question_text: str,
    options: list[str] | None,
    context: dict[str, Any] | None,
    **kwargs: Any,
) -> tuple[AnswerSuggestion, dict[str, Any]]:
    """Versions d'outils antérieures si l'API rejette les types récents."""
    if not api_available():
        raise RuntimeError("Clé API Anthropic absente.")

    user_text = build_user_message(question_text, options, context)
    user_content = _build_user_content(user_text, kwargs.get("image_paths"))
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    tools = [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 3,
            "allowed_domains": ["odoo.com", "www.odoo.com"],
        },
        {
            "type": "web_fetch_20250910",
            "name": "web_fetch",
            "max_uses": 3,
            "allowed_domains": ["odoo.com", "www.odoo.com"],
        },
    ]
    tv, ov = _resolve_target_versions(context, kwargs.get("target_version"))
    t0 = time.perf_counter()
    response = _create_messages(
        messages=messages, tools=tools, target_version=tv, other_version=ov,
        model=kwargs.get("model"),
    )
    text = extract_text_from_content(response.content)
    meta = {
        "model": getattr(response, "model", None) or kwargs.get("model") or _answer_model(),
        "latency_s": round(time.perf_counter() - t0, 3),
        "retried_json": False,
        "legacy_web_tools": True,
        "target_version": tv,
        "other_version": ov,
    }
    try:
        suggestion = apply_confidence_guard(
            parse_answer_suggestion(text), context, question_text=question_text
        )
        _log_call(suggestion, meta)
        return suggestion, meta
    except (ValueError, ValidationError, json.JSONDecodeError):
        retry_messages = messages + [
            {"role": "assistant", "content": text or "{}"},
            {"role": "user", "content": JSON_RETRY_USER_APPEND},
        ]
        response2 = _create_messages(
            messages=retry_messages, tools=tools, target_version=tv, other_version=ov,
            model=kwargs.get("model"),
        )
        text2 = extract_text_from_content(response2.content)
        meta["retried_json"] = True
        meta["latency_s"] = round(time.perf_counter() - t0, 3)
        suggestion = apply_confidence_guard(
            parse_answer_suggestion(text2), context, question_text=question_text
        )
        _log_call(suggestion, meta)
        return suggestion, meta


def translate_item_fr(
    title_en: str,
    answers_en: list[str],
    *,
    image_paths: list[str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Traductions FR (hors schéma certification) — conserve le flux capture."""
    from quiz_llm import parse_json_value, run_answer_prompt

    n = len(answers_en)
    lines = "\n".join(f"  {i + 1}. {a}" for i, a in enumerate(answers_en))
    prompt = f"""Traduis en français pour un quiz Odoo certification.

Question (EN) :
{title_en}

Options (EN) :
{lines}

Réponds UNIQUEMENT avec un objet JSON :
{{
  "title_fr": "...",
  "answers_fr": ["...", ...]
}}
Exactement {n} entrées dans answers_fr."""
    raw = run_answer_prompt(prompt, image_paths=image_paths, timeout=timeout)
    data = parse_json_value(raw)
    return data if isinstance(data, dict) else {}
