"""ジャンルごとの 1 日のまとめを、まとめ用のテキストチャンネル（既定 #ニュース）に投稿する。

まとめ = LLM が見出しの一覧から書く「今日の傾向」（2 文）＋ 重要度の高い順の投稿へのリンク。
一覧は LLM に書かせない（関係のない話題を 1 行に混ぜることがあったため）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from .. import db
from ..discord_api import (
    DIGEST_KEY,
    delete_webhook_message,
    edit_webhook_message,
    post_url,
    send_webhook,
)
from ..llm import Summarizer
from .genres import top_genre

MAX_CONTENT = 2000
# 傾向を書かせる見出しの上限（LLM の入力 8,192 トークンに収めるため）
MAX_TREND_TITLES = 150


@dataclass
class DigestItem:
    """まとめに載せる 1 記事。"""
    title: str
    thread_id: str | None
    priority: float | None
    likes: int | None


def collect(conn: sqlite3.Connection, genre: str, since: datetime) -> list[DigestItem]:
    """since 以降に投稿したジャンルの記事を、重要度の高い順に返す（判定のないものは最後）。"""
    rows = conn.execute(
        "SELECT f.category, i.title, i.discord_thread_id, i.priority, i.likes FROM items i"
        " JOIN feeds f ON f.id = i.feed_id WHERE i.status = 'notified' AND i.notified_at >= ?",
        (since.isoformat(timespec="seconds"),),
    ).fetchall()
    items = [
        DigestItem(r["title"] or "(無題)", r["discord_thread_id"], r["priority"], r["likes"])
        for r in rows
        if top_genre(r["category"]) == genre
    ]
    return sorted(items, key=lambda i: -(i.priority if i.priority is not None else -1))


def _link_text(title: str) -> str:
    # Discord の [文字](URL) の中で使えない角括弧を全角にする
    return title.replace("[", "［").replace("]", "］")


def render(trend: str | None, items: list[DigestItem], guild_id: str, top: int, limit: int = MAX_CONTENT) -> str:
    """まとめの本文（傾向＋重要度順の投稿へのリンク＋「ほか N 件」）を limit 字に収めて作る。"""
    lines = [f"{trend}\n\n**注目の記事**" if trend else "**注目の記事**"]
    for it in items[:top]:
        line = f"- [{_link_text(it.title)}]({post_url(guild_id, it.thread_id)})" if it.thread_id else f"- {it.title}"
        if it.likes:
            line += f"  👍{it.likes}"
        # 「ほか N 件」の行（最大 20 字程度）の分を残して 2,000 字に収める
        if len("\n".join([*lines, line])) + 20 > limit:
            break
        lines.append(line)
    if (rest := len(items) - (len(lines) - 1)) > 0:
        lines.append(f"ほか {rest} 件")
    return "\n".join(lines)


async def post_digest(
    conn: sqlite3.Connection,
    summarizer: Summarizer | None,
    genre: str,
    *,
    label: str | None = None,
    now: datetime,
    top: int,
    window_hours: int,
    update: bool = False,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """genre の今日のまとめを投稿し、本文を返す。見出しには label（なければ genre）を使う。

    update=True なら、投稿済みの今日のまとめを作り直して書き換える。
    投稿する記事がない・送り先がない・（投稿なら）投稿済み・（更新なら）未投稿のときは None。
    """
    day = now.astimezone().strftime("%Y-%m-%d")
    channel = _channel(conn)
    posted = _posted(conn, genre, day)
    if channel is None or (posted is not None) != update:
        return None
    items = collect(conn, genre, now - timedelta(hours=window_hours))
    if not items:
        return None
    trend = None
    if summarizer:
        trend = await summarizer.trend([i.title for i in items[:MAX_TREND_TITLES]])
    header = f"## 📰 {label or genre} {now.astimezone():%m/%d} のまとめ（{len(items)} 件）"
    content = header + "\n" + render(trend, items, channel["guild_id"], top, limit=MAX_CONTENT - len(header) - 1)
    if dry_run:
        return content
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    if update:
        edit_webhook_message(channel["webhook_url"], posted, payload, transport=transport)
        return content
    # まとめはテキストチャンネル（#ニュース）に、ジャンルごとに 1 メッセージで投稿する
    msg = send_webhook(channel["webhook_url"], {"username": "feedpipe まとめ", **payload}, transport=transport)
    with conn:
        conn.execute(
            "INSERT INTO digests (genre, day, thread_id, created_at) VALUES (?, ?, ?, ?)",
            (genre, day, msg.get("id"), db.now_iso()),
        )
    return content


def delete_digest(
    conn: sqlite3.Connection, genre: str, day: str, *, transport: httpx.BaseTransport | None = None
) -> bool:
    """day（YYYY-MM-DD）の genre のまとめを消す。投稿済みの記録も消すので、同じ日にもう一度投稿できる。"""
    channel = _channel(conn)
    message_id = _posted(conn, genre, day)
    if message_id is None:
        return False
    if channel is not None:
        delete_webhook_message(channel["webhook_url"], message_id, transport=transport)
    with conn:
        conn.execute("DELETE FROM digests WHERE genre = ? AND day = ?", (genre, day))
    return True


def _channel(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT webhook_url, guild_id FROM discord_channels WHERE category = ?", (DIGEST_KEY,)
    ).fetchone()


def _posted(conn: sqlite3.Connection, genre: str, day: str) -> str | None:
    """投稿済みのまとめのメッセージ ID（未投稿なら None）。"""
    row = conn.execute("SELECT thread_id FROM digests WHERE genre = ? AND day = ?", (genre, day)).fetchone()
    return row[0] if row else None


def is_due(now: datetime, hour: int) -> bool:
    """まとめを投稿する時刻（ローカル時刻の hour 時）を過ぎたか。"""
    return now.astimezone().hour >= hour
