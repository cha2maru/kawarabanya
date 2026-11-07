"""SQLite による状態管理（フィード・記事）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    original_url TEXT NOT NULL,
    title TEXT,
    site_url TEXT,
    category TEXT,
    source_type TEXT NOT NULL DEFAULT 'rss',
    enabled INTEGER NOT NULL DEFAULT 1,
    disabled_reason TEXT,
    etag TEXT,
    last_modified TEXT,
    last_fetched_at TEXT,
    last_success_at TEXT,
    last_status TEXT,
    last_error TEXT,
    latest_entry_at TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    options TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    url TEXT,
    title TEXT,
    published TEXT,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL,
    md_path TEXT,
    summary TEXT,
    error TEXT,
    UNIQUE (feed_id, key)
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


@dataclass
class Feed:
    id: int
    url: str
    original_url: str
    title: str | None
    site_url: str | None
    category: str | None
    enabled: bool
    disabled_reason: str | None
    last_status: str | None
    last_error: str | None
    last_success_at: str | None
    latest_entry_at: str | None
    fail_count: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Feed:
        return cls(
            id=row["id"],
            url=row["url"],
            original_url=row["original_url"],
            title=row["title"],
            site_url=row["site_url"],
            category=row["category"],
            enabled=bool(row["enabled"]),
            disabled_reason=row["disabled_reason"],
            last_status=row["last_status"],
            last_error=row["last_error"],
            last_success_at=row["last_success_at"],
            latest_entry_at=row["latest_entry_at"],
            fail_count=row["fail_count"],
        )


def find_feed_by_url(conn: sqlite3.Connection, url: str) -> Feed | None:
    """現在の URL か、取り込み時の URL のどちらかで一致するフィードを返す。"""
    row = conn.execute(
        "SELECT * FROM feeds WHERE url = ? OR original_url = ? LIMIT 1", (url, url)
    ).fetchone()
    return Feed.from_row(row) if row else None


def get_feed(conn: sqlite3.Connection, feed_id: int) -> Feed | None:
    row = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    return Feed.from_row(row) if row else None


def insert_feed(
    conn: sqlite3.Connection,
    url: str,
    title: str | None,
    site_url: str | None,
    category: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO feeds (url, original_url, title, site_url, category, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (url, url, title, site_url, category, now_iso()),
    )
    return cur.lastrowid


def list_feeds(
    conn: sqlite3.Connection,
    *,
    enabled: bool | None = None,
    category: str | None = None,
) -> list[Feed]:
    sql = "SELECT * FROM feeds WHERE 1 = 1"
    params: list[object] = []
    if enabled is not None:
        sql += " AND enabled = ?"
        params.append(int(enabled))
    if category is not None:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY category, id"
    return [Feed.from_row(r) for r in conn.execute(sql, params)]


def set_enabled(
    conn: sqlite3.Connection, feed_id: int, enabled: bool, reason: str | None = None
) -> bool:
    cur = conn.execute(
        "UPDATE feeds SET enabled = ?, disabled_reason = ?,"
        " fail_count = CASE WHEN ? THEN 0 ELSE fail_count END WHERE id = ?",
        (int(enabled), None if enabled else reason, int(enabled), feed_id),
    )
    return cur.rowcount > 0
