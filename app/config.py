#!/usr/bin/env python3
"""Configuration applicative (certification cible, etc.)."""

from __future__ import annotations

from app.doc_schema import TARGET_VERSIONS
from app.settings_db import get_setting, set_setting

KEY_TARGET_CERTIFICATION = "target_certification"
CERT_VERSIONS = frozenset({"18.0", "19.0"})
DEFAULT_TARGET_CERTIFICATION = "19.0"


def normalize_cert_version(value: str | None) -> str:
    v = (value or DEFAULT_TARGET_CERTIFICATION).strip()
    if v not in CERT_VERSIONS:
        raise ValueError(f"Version certification invalide : {v!r} (attendu 18.0 ou 19.0)")
    return v


def other_cert_version(version: str) -> str:
    v = normalize_cert_version(version)
    return "18.0" if v == "19.0" else "19.0"


def get_target_certification() -> str:
    raw = get_setting(KEY_TARGET_CERTIFICATION, DEFAULT_TARGET_CERTIFICATION)
    try:
        return normalize_cert_version(raw)
    except ValueError:
        return DEFAULT_TARGET_CERTIFICATION


def set_target_certification(version: str) -> str:
    v = normalize_cert_version(version)
    set_setting(KEY_TARGET_CERTIFICATION, v)
    return v


def question_matches_target_cert(q: dict, cert_version: str | None = None) -> bool:
    """True si la question est visible pour l'entraînement certification donnée."""
    cert = normalize_cert_version(cert_version) if cert_version else get_target_certification()
    tv = q.get("target_version")
    if tv is None or (isinstance(tv, str) and not tv.strip()):
        return False
    tv = str(tv).strip()
    return tv == cert or tv == "both"


def filter_questions_for_cert(
    questions: list[dict],
    cert_version: str | None = None,
) -> list[dict]:
    return [q for q in questions if isinstance(q, dict) and question_matches_target_cert(q, cert_version)]


def count_questions_for_cert(
    questions: list[dict],
    cert_version: str | None = None,
) -> dict[str, int]:
    cert = normalize_cert_version(cert_version) if cert_version else get_target_certification()
    matched = filter_questions_for_cert(questions, cert)
    by_tv: dict[str, int] = {}
    for q in questions:
        if not isinstance(q, dict):
            continue
        tv = q.get("target_version")
        key = str(tv).strip() if tv is not None and str(tv).strip() else "(non classé)"
        by_tv[key] = by_tv.get(key, 0) + 1
    return {
        "cert_version": cert,
        "matched": len(matched),
        "total_bank": len([q for q in questions if isinstance(q, dict)]),
        "by_target_version": by_tv,
    }
