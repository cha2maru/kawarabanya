"""SQLite による状態管理（フィード・記事）。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
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
    -- 初回取り込み（既存記事の既読化）を済ませた日時。NULL なら次の run で既読化だけ行う
    initialized_at TEXT,
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

-- 投稿済みの 1 日のまとめ（同じ日に二重に投稿しないため）
CREATE TABLE IF NOT EXISTS digests (
    genre TEXT NOT NULL,
    day TEXT NOT NULL,          -- ローカル日付 YYYY-MM-DD
    thread_id TEXT,             -- 投稿したメッセージの ID
    created_at TEXT NOT NULL,
    PRIMARY KEY (genre, day)
);

-- feedpipe のカテゴリ → Discord のチャンネルと Webhook
CREATE TABLE IF NOT EXISTS discord_channels (
    category TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    tags TEXT,  -- JSON: {feed_id: タグ ID}
    created_at TEXT NOT NULL
);
"""


# 既存の DB に後から足した列（CREATE TABLE IF NOT EXISTS では追加されないため）
ADDED_COLUMNS = {
    "feeds": {"latest_entry_at": "TEXT", "initialized_at": "TEXT"},
    "items": {"notified_at": "TEXT", "discord_thread_id": "TEXT", "likes": "INTEGER", "likes_checked_at": "TEXT", "priority": "REAL"},
    "discord_channels": {"tags": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


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
    _migrate(conn)
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
    etag: str | None
    last_modified: str | None
    initialized_at: str | None
    options: dict = field(default_factory=dict)

    def option(self, name: str, default: object = None) -> object:
        return self.options.get(name, default)

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
            etag=row["etag"],
            last_modified=row["last_modified"],
            initialized_at=row["initialized_at"],
            options=json.loads(row["options"]) if row["options"] else {},
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


def insert_item(
    conn: sqlite3.Connection,
    feed_id: int,
    key: str,
    url: str | None,
    title: str | None,
    published: str | None,
    status: str,
) -> int | None:
    """記事を登録する。登録済み（既読）なら None を返す。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO items (feed_id, key, url, title, published, fetched_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (feed_id, key, url, title, published, now_iso(), status),
    )
    return cur.lastrowid if cur.rowcount else None


def update_item(conn: sqlite3.Connection, item_id: int, **values: object) -> None:
    cols = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE items SET {cols} WHERE id = ?", (*values.values(), item_id))


def mark_initialized(conn: sqlite3.Connection, feed_id: int) -> None:
    conn.execute("UPDATE feeds SET initialized_at = ? WHERE id = ?", (now_iso(), feed_id))


def set_option(conn: sqlite3.Connection, feed_id: int, name: str, value: object) -> bool:
    """フィード個別の設定を変える。value が None なら削除する。"""
    feed = get_feed(conn, feed_id)
    if feed is None:
        return False
    options = dict(feed.options)
    if value is None:
        options.pop(name, None)
    else:
        options[name] = value
    conn.execute(
        "UPDATE feeds SET options = ? WHERE id = ?",
        (json.dumps(options, ensure_ascii=False) if options else None, feed_id),
    )
    return True
