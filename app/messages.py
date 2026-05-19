#!/usr/bin/env python3
"""Construction des messages utilisateur pour Claude."""

from __future__ import annotations

from typing import Any


def _format_options(options: list[str]) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lines: list[str] = []
    for i, opt in enumerate(options):
        if i >= len(letters):
            break
        letter = letters[i]
        lines.append(f"{letter}. {(opt or '').strip()}")
    return "\n".join(lines)


def _format_doc_chunks(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return ""
    parts: list[str] = []
    for i, ch in enumerate(chunks, start=1):
        url = (ch.get("url") or "").strip()
        title = (ch.get("title") or ch.get("section") or "").strip()
        text = (ch.get("text") or "").strip()
        ver = (ch.get("version") or "").strip()
        ver_tag = f" [{ver}]" if ver else ""
        header = f"[{i}]{ver_tag} {url} — {title}".strip(" —")
        parts.append(f"{header}\n{text}")
    return "\n---\n\n".join(parts)


def _version_tag(tv: str | None) -> str:
    v = (tv or "").strip()
    if v == "19.0":
        return "[v19]"
    if v == "18.0":
        return "[v18]"
    if v == "both":
        return "[v18+v19]"
    return "[?]"


def _format_similar_qas(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    parts: list[str] = []
    for i, row in enumerate(rows, start=1):
        tag = _version_tag(row.get("target_version"))
        q = (row.get("title") or "").strip()
        ci = row.get("correct_index")
        opts = row.get("options") or []
        ans = ""
        if isinstance(ci, int) and 1 <= ci <= len(opts):
            ans = (opts[ci - 1] or "").strip()
        elif row.get("correct_text"):
            ans = (row.get("correct_text") or "").strip()
        parts.append(f"[{i}] {tag} Q: {q}\n   R: {ans}")
    return "\n\n".join(parts)


def build_user_message(
    question_text: str,
    options: list[str] | None,
    context: dict[str, Any] | None,
) -> str:
    """Message utilisateur structuré (balises XML)."""
    ctx = context or {}
    blocks: list[str] = []

    q = (question_text or "").strip()
    blocks.append(f"<question>\n{q}\n</question>")

    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if opts:
        blocks.append(f"<options>\n{_format_options(opts)}\n</options>")

    tv = (ctx.get("target_version") or "").strip()
    if tv:
        blocks.append(f"<certification_cible>\nOdoo {tv}\n</certification_cible>")

    doc_xml = _format_doc_chunks(ctx.get("doc_chunks") or [])
    if doc_xml:
        blocks.append(f"<doc_chunks>\n{doc_xml}\n</doc_chunks>")

    sim_xml = _format_similar_qas(ctx.get("similar_qas") or [])
    if sim_xml:
        blocks.append(f"<similar_qas>\n{sim_xml}\n</similar_qas>")

    blocks.append(
        "Réponds en JSON strict selon le schéma défini dans le system prompt."
    )
    return "\n\n".join(blocks)


def _format_doc_chunks_tagged(chunks: list[dict[str, Any]], tag: str) -> str:
    if not chunks:
        return "(aucun extrait trouvé)"
    parts: list[str] = []
    for i, ch in enumerate(chunks, start=1):
        url = (ch.get("url") or "").strip()
        title = (ch.get("title") or ch.get("section") or "").strip()
        text = (ch.get("text") or "").strip()
        header = f"[{i}] {url} — {title}".strip(" —")
        parts.append(f"{header}\n{text}")
    return "\n---\n\n".join(parts)


def build_classification_user_message(
    question_text: str,
    options: list[str],
    correct_answer: str,
    doc_v18: list[dict[str, Any]],
    doc_v19: list[dict[str, Any]],
) -> str:
    """Message utilisateur pour scripts/classify_versions.py."""
    blocks = [
        f"<question>\n{(question_text or '').strip()}\n</question>",
        f"<options>\n{_format_options(options)}\n</options>",
        f"<bonne_reponse>\n{(correct_answer or '').strip()}\n</bonne_reponse>",
        f"<doc_v18>\n{_format_doc_chunks_tagged(doc_v18, 'v18')}\n</doc_v18>",
        f"<doc_v19>\n{_format_doc_chunks_tagged(doc_v19, 'v19')}\n</doc_v19>",
        "Classifie cette question en JSON selon le schéma défini dans le system prompt.",
    ]
    return "\n\n".join(blocks)
