#!/usr/bin/env python3
"""Tests parsing JSON suggestion (sans API)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.llm import parse_answer_suggestion, reponse_to_correct_index


def test_parse_clean_json():
    raw = '{"reponse":"B","justification":"Parce que.","confiance":"haute","sources":[],"alerte_version":false}'
    s = parse_answer_suggestion(raw)
    assert s.reponse == "B"
    assert s.confiance == "haute"


def test_parse_fenced_json():
    raw = '```json\n{"reponse":"A","justification":"x","confiance":"moyenne","sources":[],"alerte_version":false}\n```'
    s = parse_answer_suggestion(raw)
    assert s.reponse == "A"


def test_reponse_to_index():
    assert reponse_to_correct_index("C", 4) == 3
    assert reponse_to_correct_index("A,C", 4) == 1
