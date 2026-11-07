"""ジャンルごとの 1 日のまとめを、まとめ用のテキストチャンネル（既定 #ニュース）に投稿する。

まとめ = LLM が見出しの一覧から書く「今日の傾向」（2 文）＋ 重要度の高い順の投稿へのリンク。
一覧は LLM に書かせない（関係のない話題を 1 行に混ぜることがあったため）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from . import db
from .discord_api import DIGEST_KEY, post_url, send_webhook
from .genres import top_genre
from .summarize import Summarizer

MAX_CONTENT = 2000
# 傾向を書かせる見出しの上限（LLM の入力 8,192 トークンに収めるため）
MAX_TREND_TITLES = 150


@dataclass
class DigestItem:
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


def already_posted(conn: sqlite3.Connection, genre: str, day: str) -> bool:
    return conn.execute("SELECT 1 FROM digests WHERE genre = ? AND day = ?", (genre, day)).fetchone() is not None


async def post_digest(
    conn: sqlite3.Connection,
    summarizer: Summarizer | None,
    genre: str,
    *,
    now: datetime,
    top: int,
    window_hours: int,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """genre のまとめを投稿し、本文を返す。投稿する記事がない・送り先がない・投稿済みなら None。"""
    day = now.astimezone().strftime("%Y-%m-%d")
    channel = conn.execute(
        "SELECT webhook_url, guild_id FROM discord_channels WHERE category = ?", (DIGEST_KEY,)
    ).fetchone()
    if channel is None or already_posted(conn, genre, day):
        return None
    items = collect(conn, genre, now - timedelta(hours=window_hours))
    if not items:
        return None
    trend = None
    if summarizer:
        trend = await summarizer.trend([i.title for i in items[:MAX_TREND_TITLES]])
    header = f"## 📰 {genre} {now.astimezone():%m/%d} のまとめ（{len(items)} 件）"
    content = header + "\n" + render(trend, items, channel["guild_id"], top, limit=MAX_CONTENT - len(header) - 1)
    if dry_run:
        return content
    # まとめはテキストチャンネル（#ニュース）に、ジャンルごとに 1 メッセージで投稿する
    msg = send_webhook(channel["webhook_url"], {
        "username": "feedpipe まとめ",
        "content": content,
        "allowed_mentions": {"parse": []},
    }, transport=transport)
    with conn:
        conn.execute(
            "INSERT INTO digests (genre, day, thread_id, created_at) VALUES (?, ?, ?, ?)",
            (genre, day, msg.get("id"), db.now_iso()),
        )
    return content


def is_due(now: datetime, hour: int) -> bool:
    return now.astimezone().hour >= hour
