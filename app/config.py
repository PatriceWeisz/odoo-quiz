#!/usr/bin/env python3
"""Configuration applicative (certification cible, etc.)."""

from __future__ import annotations

import json
from pathlib import Path

from app.doc_schema import TARGET_VERSIONS
from app.settings_db import get_setting, set_setting
from app.study_modules import STUDY_MODULES, tier_of

KEY_TARGET_CERTIFICATION = "target_certification"
CERT_VERSIONS = frozenset({"18.0", "19.0"})
DEFAULT_TARGET_CERTIFICATION = "19.0"

# Status considéré comme "à exclure par défaut" du quiz (questions générées
# avec score judge=3 — gardées dans la banque mais cachées par défaut).
HIDDEN_STATUSES_DEFAULT = frozenset({"unverified", "flagged"})

# Map qid_udemy → module (inférée par scripts/build_udemy_module_map.py).
# Chargée une fois, mise en cache module-level.
_UDEMY_MODULES_FILE = Path(__file__).resolve().parent.parent / "data" / "udemy_modules.json"
_udemy_modules_cache: dict[int, str] | None = None


def _load_udemy_modules_map() -> dict[int, str]:
    """Charge data/udemy_modules.json (lazy, cache module-level)."""
    global _udemy_modules_cache
    if _udemy_modules_cache is not None:
        return _udemy_modules_cache
    out: dict[int, str] = {}
    if _UDEMY_MODULES_FILE.exists():
        try:
            raw = json.loads(_UDEMY_MODULES_FILE.read_text(encoding="utf-8"))
            for qid_str, rec in raw.items():
                if isinstance(rec, dict):
                    mod = rec.get("module")
                    if mod and not rec.get("below_threshold"):
                        try:
                            out[int(qid_str)] = str(mod)
                        except (TypeError, ValueError):
                            pass
        except (OSError, json.JSONDecodeError):
            pass
    _udemy_modules_cache = out
    return out


def question_module(q: dict) -> str | None:
    """Module associé à une question, soit explicite (q['module']) soit
    inféré pour les Udemy via data/udemy_modules.json."""
    explicit = (q.get("module") or "").strip()
    if explicit:
        return explicit
    qid = q.get("id")
    if isinstance(qid, int):
        return _load_udemy_modules_map().get(qid)
    return None


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


def question_matches_module(q: dict, module: str | None) -> bool:
    """True si la question correspond au module donné (ou si module=None)."""
    if not module:
        return True
    qmod = question_module(q)
    return qmod == module


def question_is_visible(
    q: dict,
    *,
    cert_version: str | None = None,
    module: str | None = None,
    include_hidden: bool = False,
) -> bool:
    """Combine tous les filtres : cert + module + status (exclude unverified par défaut)."""
    if not question_matches_target_cert(q, cert_version):
        return False
    if not question_matches_module(q, module):
        return True if module is None else False
    if not include_hidden:
        st = (q.get("status") or "").strip()
        if st in HIDDEN_STATUSES_DEFAULT:
            return False
    return True


def filter_questions_for_cert(
    questions: list[dict],
    cert_version: str | None = None,
    *,
    module: str | None = None,
    include_hidden: bool = False,
) -> list[dict]:
    return [
        q for q in questions
        if isinstance(q, dict)
        and question_is_visible(q, cert_version=cert_version, module=module,
                                include_hidden=include_hidden)
    ]


def count_questions_for_cert(
    questions: list[dict],
    cert_version: str | None = None,
    *,
    module: str | None = None,
    include_hidden: bool = False,
) -> dict[str, int]:
    cert = normalize_cert_version(cert_version) if cert_version else get_target_certification()
    matched = filter_questions_for_cert(
        questions, cert, module=module, include_hidden=include_hidden,
    )
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
        "module": module,
        "include_hidden": include_hidden,
    }


def list_modules_with_counts(
    questions: list[dict],
    cert_version: str | None = None,
    *,
    include_hidden: bool = False,
) -> list[dict]:
    """Pour chaque module de STUDY_MODULES, compte les questions disponibles
    (filtrage cert + status). Retourne aussi les modules avec count=0
    pour donner une vue d'ensemble.
    """
    cert = normalize_cert_version(cert_version) if cert_version else get_target_certification()
    out: list[dict] = []
    for tier_name, modules in STUDY_MODULES.items():
        for m in modules:
            n = sum(
                1 for q in questions
                if isinstance(q, dict)
                and question_is_visible(q, cert_version=cert, module=m, include_hidden=include_hidden)
            )
            out.append({"module": m, "tier": tier_name, "count": n})
    # Comptage "autres" (Udemy non mappés sur un module connu)
    n_other = sum(
        1 for q in questions
        if isinstance(q, dict)
        and question_matches_target_cert(q, cert)
        and not question_module(q)
        and ((q.get("status") or "") not in HIDDEN_STATUSES_DEFAULT or include_hidden)
    )
    if n_other > 0:
        out.append({"module": "_unclassified", "tier": "other", "count": n_other})
    return out
