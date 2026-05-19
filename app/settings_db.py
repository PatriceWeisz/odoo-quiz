#!/usr/bin/env python3
"""Réglages applicatifs persistés (SQLite) — modifiables sans redémarrage."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_DB = ROOT / "data" / "app_settings.sqlite"

_lock = threading.Lock()


def init_settings_db() -> None:
    SETTINGS_DB.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = sqlite3.connect(SETTINGS_DB)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    init_settings_db()
    with _lock:
        conn = sqlite3.connect(SETTINGS_DB)
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return default
    return row[0]


def set_setting(key: str, value: str) -> None:
    init_settings_db()
    with _lock:
        conn = sqlite3.connect(SETTINGS_DB)
        try:
            conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()
