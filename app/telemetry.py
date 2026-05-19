#!/usr/bin/env python3
"""Journal JSONL des suggestions Claude."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "suggestions.log"


def log_suggestion(
    *,
    question_id: int | str | None,
    model: str | None,
    confiance: str | None,
    sources: list[dict[str, Any]] | None,
    latency_s: float | None,
    input_tokens: int | None,
    output_tokens: int | None,
    target_version: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "question_id": question_id,
        "model": model,
        "confiance": confiance,
        "sources": sources or [],
        "latency_s": latency_s,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "target_version": target_version,
    }
    if extra:
        row.update(extra)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
