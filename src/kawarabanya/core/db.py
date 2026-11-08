"""SQLite による状態管理（フィード・記事）と、DB のマイグレーション。

DB の版は PRAGMA user_version。connect が MIGRATIONS のうち未適用のものを番号順に 1 回ずつ適用する
（既存の DB なら、その前に <DB>.bak-v<旧版> にバックアップを取る）。
スキーマを変えるときは MIGRATIONS の末尾に関数を足す（SCHEMA と既存のマイグレーションは変えない）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# 版 1 のテーブル（ここは変えない。変えるときは MIGRATIONS に足す）
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

-- 投稿済みのまとめ（day は投稿したローカル日時。書き換え・削除と、前回のまとめ以降の記事を選ぶため）
CREATE TABLE IF NOT EXISTS digests (
    genre TEXT NOT NULL,
    day TEXT NOT NULL,          -- ローカル日付 YYYY-MM-DD
    thread_id TEXT,             -- 投稿したメッセージの ID
    created_at TEXT NOT NULL,
    PRIMARY KEY (genre, day)
);

-- 定期処理の最後の実行日など、小さな状態（key → value）
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- kawarabanya のカテゴリ → Discord のチャンネルと Webhook
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


# 版 1 より前（0.1 の開発中）に後から足した列。版 1 のマイグレーションで、なければ足す
ADDED_COLUMNS = {
    "feeds": {"latest_entry_at": "TEXT", "initialized_at": "TEXT"},
    "items": {
        "notified_at": "TEXT", "discord_thread_id": "TEXT", "likes": "INTEGER", "likes_checked_at": "TEXT",
        "priority": "REAL", "subgenre": "TEXT", "content_hash": "TEXT",
    },
    "discord_channels": {"tags": "TEXT"},
}


def _v1(conn: sqlite3.Connection) -> None:
    """版 1: テーブルを作り、開発中に後から足した列をそろえる。"""
    conn.executescript(SCHEMA)
    for table, columns in ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# (説明, 関数)。i 番目（0 始まり）を適用すると版が i + 1 になる。末尾に足すだけで、並びも中身も変えない
def _v2(conn: sqlite3.Connection) -> None:
    """版 2: 日本語以外の記事の見出しの訳（items.title_ja）。"""
    conn.execute("ALTER TABLE items ADD COLUMN title_ja TEXT")


def _v3(conn: sqlite3.Connection) -> None:
    """版 3: Google カレンダーの予定（schedule/events.py。前回と比べて変更を知らせる・直前に知らせる）。"""
    conn.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, calendar TEXT NOT NULL, summary TEXT NOT NULL, start TEXT NOT NULL,"
        " end TEXT NOT NULL, all_day INTEGER NOT NULL, location TEXT, updated TEXT, reminded_at TEXT)"
    )


MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection], None]]] = [
    ("テーブルの作成（0.1）", _v1),
    ("見出しの訳の列（items.title_ja）", _v2),
    ("予定のテーブル（events）", _v3),
]


def schema_version(conn: sqlite3.Connection) -> int:
    """DB の版（PRAGMA user_version）。"""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, path: Path | None = None) -> list[str]:
    """未適用のマイグレーションを番号順に適用し、適用したものの説明を返す。

    path があり、既存の DB（版 1 以上）を上げるときは、先に path.bak-v<旧版> にバックアップを取る。
    DB の版がこのコードより新しければ ValueError（古い kawarabanya で新しい DB を壊さないため）。
    """
    current, latest = schema_version(conn), len(MIGRATIONS)
    if current > latest:
        raise ValueError(f"DB の版 {current} はこの kawarabanya（版 {latest} まで）より新しいです。kawarabanya を更新してください")
    if current == latest:
        return []
    if path is not None and current > 0:
        backup = path.with_name(f"{path.name}.bak-v{current}")
        with closing(sqlite3.connect(backup)) as dst:
            conn.backup(dst)
        log.info("DB を版 %d から上げる前にバックアップしました: %s", current, backup)
    applied = []
    for version, (desc, fn) in enumerate(MIGRATIONS[current:], start=current + 1):
        with conn:
            fn(conn)
            conn.execute(f"PRAGMA user_version = {version}")
        log.info("DB のマイグレーション: 版 %d（%s）", version, desc)
        applied.append(desc)
    return applied


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    """state テーブルの値（なければ None）。"""
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """state テーブルに値を書く。"""
    with conn:
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))


def now_iso() -> str:
    """現在時刻（UTC）を ISO 8601 の文字列で返す。DB に保存する日時はすべてこの形式。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """DB を開き、未適用のマイグレーションを適用する。親ディレクトリがなければ作る。"""
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn, None if str(path) == ":memory:" else path)
    return conn


@dataclass
class Feed:
    """feeds テーブルの 1 行。"""
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
    source_type: str = "rss"  # rss / html（HTML 取り込み）/ 拡張の名前（設定は options[source_type]）

    def option(self, name: str, default: object = None) -> object:
        """フィード個別の設定（feeds option で設定）を返す。"""
        return self.options.get(name, default)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Feed:
        """SELECT * FROM feeds の行から作る。"""
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
            source_type=row["source_type"],
        )


def find_feed_by_url(conn: sqlite3.Connection, url: str) -> Feed | None:
    """現在の URL か、取り込み時の URL のどちらかで一致するフィードを返す。"""
    row = conn.execute(
        "SELECT * FROM feeds WHERE url = ? OR original_url = ? LIMIT 1", (url, url)
    ).fetchone()
    return Feed.from_row(row) if row else None


def get_feed(conn: sqlite3.Connection, feed_id: int) -> Feed | None:
    """ID でフィードを返す。なければ None。"""
    row = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    return Feed.from_row(row) if row else None


def insert_feed(
    conn: sqlite3.Connection,
    url: str,
    title: str | None,
    site_url: str | None,
    category: str | None,
) -> int:
    """フィードを登録し、ID を返す。取り込み時の URL を original_url にも残す。"""
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
    """フィードを、有効・無効とカテゴリで絞って返す（カテゴリ・ID 順）。"""
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
    """フィードを有効・無効にする。有効に戻すときは失敗回数を 0 にする。見つからなければ False。"""
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
    """記事（items）の列を、キーワード引数で指定した値に更新する。"""
    cols = ", ".join(f"{k} = ?" for k in values)
    conn.execute(f"UPDATE items SET {cols} WHERE id = ?", (*values.values(), item_id))


def mark_initialized(conn: sqlite3.Connection, feed_id: int) -> None:
    """フィードの初回取り込み（既存記事の既読化）が済んだことを記録する。"""
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
