#!/usr/bin/env python3
"""Filtres questions banque par version certification (réexport)."""

from app.config import (
    count_questions_for_cert,
    filter_questions_for_cert,
    get_target_certification,
    question_matches_target_cert,
)

__all__ = [
    "count_questions_for_cert",
    "filter_questions_for_cert",
    "get_target_certification",
    "question_matches_target_cert",
]
