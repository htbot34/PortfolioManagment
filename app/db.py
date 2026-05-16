"""Tiny SQLite layer for caching recommendation history."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            action TEXT,
            horizon TEXT,
            conviction INTEGER,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recs_ticker ON recommendations(ticker);
        CREATE INDEX IF NOT EXISTS idx_recs_created ON recommendations(created_at);
        """)


def save_recommendation(rec: dict) -> None:
    init()
    with _conn() as c:
        c.execute(
            "INSERT INTO recommendations (created_at, ticker, action, horizon, conviction, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                rec.get("ticker", ""),
                rec.get("action"),
                rec.get("horizon"),
                rec.get("conviction"),
                json.dumps(rec, default=str),
            ),
        )


def latest_recommendations(limit: int = 50) -> list[dict]:
    init()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, created_at, ticker, action, horizon, conviction, payload "
            "FROM recommendations ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def ticker_history(ticker: str, limit: int = 20) -> list[dict]:
    init()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, created_at, action, horizon, conviction, payload "
            "FROM recommendations WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
