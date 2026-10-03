from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import settings

_lock = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, thread_id TEXT, user_id TEXT,
  question TEXT, standalone TEXT, sql TEXT, summary TEXT, row_count INTEGER, tables TEXT);
CREATE INDEX IF NOT EXISTS ix_turns_thread ON turns(thread_id, id);
CREATE INDEX IF NOT EXISTS ix_turns_user ON turns(user_id, id);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, thread_id TEXT, user_id TEXT, role TEXT,
  question TEXT, sql TEXT, status TEXT, error TEXT, row_count INTEGER, latency_ms INTEGER);
"""


def _conn() -> sqlite3.Connection:
    Path(settings.memory_db).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.memory_db, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(_SCHEMA)
    return c


def add_turn(thread_id: str, user_id: str, question: str, standalone: str, sql: str, summary: str,
             row_count: int, tables: list[str]) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO turns(ts,thread_id,user_id,question,standalone,sql,summary,row_count,tables) VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), thread_id, user_id, question, standalone, sql, summary[:600], row_count, json.dumps(tables)))


def recent(thread_id: str, n: int = 4) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT question,standalone,sql,summary,row_count FROM turns WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                         (thread_id, n)).fetchall()
    return [dict(r) for r in reversed(rows)]


def user_history(user_id: str, n: int = 20) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT ts,thread_id,question,standalone,summary FROM turns WHERE user_id=? ORDER BY id DESC LIMIT ?",
                         (user_id, n)).fetchall()
    return [dict(r) for r in rows]


def audit(thread_id: str, user_id: str, role: str, question: str, sql: str, status: str, error: str = "",
          row_count: int = 0, latency_ms: int = 0) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT INTO audit(ts,thread_id,user_id,role,question,sql,status,error,row_count,latency_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (time.time(), thread_id, user_id, role, question, sql, status, error[:500], row_count, latency_ms))
