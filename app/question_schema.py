#!/usr/bin/env python3
"""Schéma étendu d'une question dans `questions.json` — Phase 5.

Les questions existantes (Udemy / claude / user) gardent leur schéma actuel
sans migration physique. Les questions GÉNÉRÉES par le pipeline Phase 5
recevront en plus les champs ci-dessous (la lecture par l'app actuelle les
ignore — c'est ok).

Champs supplémentaires (tous OPTIONNELS pour la rétrocompat) :
  - module           : libellé module (ex. "sales/point_of_sale") issu de app.study_modules
  - tier             : "cert" | "tier1" | "tier2"
  - difficulty       : "facile" | "moyen" | "difficile"
  - scenario_based   : bool  — True si la question pose un scénario contextuel
  - source           : "generated" (à distinguer de correct_answer_source qui
                       indique d'où vient la "bonne réponse")
  - source_chunk_id  : ID du chunk doc Odoo qui a servi de base
  - source_chunk_url : URL canonique de la page doc
  - evidence_snippet : extrait textuel 50-150 mots du chunk qui justifie la réponse
  - created_at       : timestamp ISO 8601 UTC
  - judge_score      : 1-5 (min des 5 critères du judge)
  - judge_decision   : "accept" | "review" | "reject"
  - judge_reasons    : list[str] — courtes raisons par critère
  - status           : "verified" | "verified_by_judge" | "unverified" | "flagged"

Helpers :
  - REQUIRED_FIELDS : tuple des champs **obligatoires** (existant) qui doivent
    rester présents quelle que soit l'origine de la question.
  - EXTENDED_FIELDS : tuple des champs **étendus** Phase 5.
  - new_generated_question(...) : factory qui produit un dict conforme avec
    valeurs par défaut pour les champs étendus.
  - validate_generated_question(q) : vérifie qu'une question générée est bien
    formée (titre, exactement 1 bonne réponse, 3 ou 4 options, etc.).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

VALID_TIERS = ("cert", "tier1", "tier2")
VALID_DIFFICULTIES = ("facile", "moyen", "difficile")
VALID_JUDGE_DECISIONS = ("accept", "review", "reject")
VALID_STATUSES = ("verified", "verified_by_judge", "unverified", "flagged")
VALID_TARGET_VERSIONS = ("18.0", "19.0", "both", None)

REQUIRED_FIELDS = (
    "id",
    "title",
    "type",
    "is_scored",
    "answers",
)

EXTENDED_FIELDS = (
    "module",
    "tier",
    "difficulty",
    "scenario_based",
    "source",
    "source_chunk_id",
    "source_chunk_url",
    "evidence_snippet",
    "created_at",
    "judge_score",
    "judge_decision",
    "judge_reasons",
    "status",
)


def now_iso() -> str:
    """Timestamp ISO 8601 UTC, précision seconde, suffixe Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_generated_question(
    *,
    qid: int,
    title: str,
    title_fr: str,
    answers: list[dict[str, Any]],
    module: str,
    tier: str,
    difficulty: str,
    scenario_based: bool,
    target_version: str | None,
    source_chunk_id: str,
    source_chunk_url: str,
    evidence_snippet: str,
    explication_claude: str = "",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Construit une question générée prête à insérer dans questions.json.

    `answers` : liste de dicts {"id": int, "value": str, "value_fr": str,
                "is_correct": bool, "score": float}. Exactement 1 is_correct=True.
    """
    if tier not in VALID_TIERS:
        raise ValueError(f"tier invalide : {tier!r}")
    if difficulty not in VALID_DIFFICULTIES:
        raise ValueError(f"difficulty invalide : {difficulty!r}")
    if target_version not in VALID_TARGET_VERSIONS:
        raise ValueError(f"target_version invalide : {target_version!r}")

    q: dict[str, Any] = {
        "id": int(qid),
        "title": title.strip(),
        "title_fr": (title_fr or "").strip(),
        "type": "simple_choice",
        "is_scored": True,
        "answers": list(answers),
        "explication_senedoo": "",
        "explication_claude": (explication_claude or "").strip(),
        "correct_answer_source": "claude",
        "target_version": target_version,
        "question_image": "",
        # Champs étendus Phase 5
        "module": module,
        "tier": tier,
        "difficulty": difficulty,
        "scenario_based": bool(scenario_based),
        "source": "generated",
        "source_chunk_id": source_chunk_id,
        "source_chunk_url": source_chunk_url,
        "evidence_snippet": evidence_snippet,
        "created_at": created_at or now_iso(),
        # Champs renseignés après pipeline judge (Phase 5.5)
        "judge_score": None,
        "judge_decision": None,
        "judge_reasons": [],
        "status": "unverified",
    }
    return q


def validate_generated_question(q: dict[str, Any]) -> list[str]:
    """Retourne une liste d'erreurs (vide si OK). Lève rien — utilisable comme filtre."""
    errors: list[str] = []
    if not isinstance(q, dict):
        return ["question n'est pas un dict"]
    for f in REQUIRED_FIELDS:
        if f not in q:
            errors.append(f"champ obligatoire manquant : {f}")

    title = (q.get("title") or "").strip()
    if len(title) < 10:
        errors.append("title trop court (<10 chars)")

    answers = q.get("answers") or []
    if not isinstance(answers, list):
        errors.append("answers doit être une liste")
        return errors
    if len(answers) not in (3, 4):
        errors.append(f"answers doit contenir 3 ou 4 options (vu {len(answers)})")
    n_correct = sum(1 for a in answers if isinstance(a, dict) and a.get("is_correct"))
    if n_correct != 1:
        errors.append(f"exactement 1 bonne réponse attendue (vu {n_correct})")

    if q.get("type") != "simple_choice":
        errors.append("type doit valoir 'simple_choice'")

    tier = q.get("tier")
    if tier is not None and tier not in VALID_TIERS:
        errors.append(f"tier invalide : {tier!r}")
    diff = q.get("difficulty")
    if diff is not None and diff not in VALID_DIFFICULTIES:
        errors.append(f"difficulty invalide : {diff!r}")
    tv = q.get("target_version")
    if tv not in VALID_TARGET_VERSIONS:
        errors.append(f"target_version invalide : {tv!r}")

    snip = (q.get("evidence_snippet") or "").strip()
    if q.get("source") == "generated":
        # evidence_snippet requis pour les questions générées
        words = len(re.findall(r"\w+", snip))
        if words < 30 or words > 200:
            errors.append(
                f"evidence_snippet hors-borne : {words} mots (attendu 30-200)"
            )
        if not q.get("source_chunk_id"):
            errors.append("source_chunk_id manquant pour une question 'generated'")

    return errors


def is_compatible_with_existing(q: dict[str, Any]) -> bool:
    """Sanity check : la nouvelle question respecte les contraintes existantes
    de l'app (cf. app.py::_bank_put_answers).
    """
    return not validate_generated_question(q)


__all__ = [
    "VALID_TIERS",
    "VALID_DIFFICULTIES",
    "VALID_JUDGE_DECISIONS",
    "VALID_STATUSES",
    "VALID_TARGET_VERSIONS",
    "REQUIRED_FIELDS",
    "EXTENDED_FIELDS",
    "now_iso",
    "new_generated_question",
    "validate_generated_question",
    "is_compatible_with_existing",
]
