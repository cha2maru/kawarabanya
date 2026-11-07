"""保存済みの記事を Discord のフォーラムに投稿する（1 記事 = 1 投稿）。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import db
from .discord_api import UNCATEGORIZED, DiscordError, send_webhook

log = logging.getLogger(__name__)

MAX_THREAD_NAME = 100
MAX_CONTENT = 2000
MAX_USERNAME = 80
EXCERPT_CHARS = 200
# Webhook のレート制限（5 件 / 2 秒）に余裕を持たせる間隔
SEND_INTERVAL = 0.5


@dataclass
class Pending:
    item_id: int
    feed_id: int
    feed_title: str | None
    title: str | None
    url: str | None
    published: str | None
    summary: str | None
    md_path: str | None
    webhook_url: str
    tags: dict[str, str]


@dataclass
class NotifyStats:
    sent: int = 0
    failed: int = 0
    no_channel: int = 0  # 送り先のチャンネルが未登録
    remaining: int = 0  # 上限に達したため次回に回したもの
    errors: list[str] = field(default_factory=list)


def pending_items(conn: sqlite3.Connection) -> tuple[list[Pending], int]:
    """通知待ちの記事と、送り先がないため送れない記事の数を返す。"""
    rows = conn.execute(
        "SELECT i.id, i.feed_id, f.title AS feed_title, i.title, i.url, i.published, i.summary,"
        " i.md_path, d.webhook_url, d.tags"
        " FROM items i JOIN feeds f ON f.id = i.feed_id"
        " LEFT JOIN discord_channels d ON d.category = COALESCE(f.category, ?)"
        " WHERE i.status = 'saved' ORDER BY i.published, i.id",
        (UNCATEGORIZED,),
    ).fetchall()
    ready = [
        Pending(
            item_id=r["id"],
            feed_id=r["feed_id"],
            feed_title=r["feed_title"],
            title=r["title"],
            url=r["url"],
            published=r["published"],
            summary=r["summary"],
            md_path=r["md_path"],
            webhook_url=r["webhook_url"],
            tags=json.loads(r["tags"]) if r["tags"] else {},
        )
        for r in rows
        if r["webhook_url"]
    ]
    return ready, len(rows) - len(ready)


def excerpt(md_path: str | None, limit: int = EXCERPT_CHARS) -> str:
    """保存した Markdown の本文から、先頭の数百字をプレーンテキストで取り出す。"""
    if not md_path or not Path(md_path).exists():
        return ""
    text = Path(md_path).read_text(encoding="utf-8")
    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[-1]
    text = text.split("\n---\n\n元記事:", 1)[0]
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # 画像
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # リンクは文字だけ
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    plain = re.sub(r"[*_`>]+", "", " ".join(lines))
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain if len(plain) <= limit else plain[:limit].rstrip() + "…"


def _username(feed_title: str | None) -> str:
    # Webhook の表示名に "discord" は使えない
    name = (feed_title or "feedpipe").strip()[:MAX_USERNAME]
    return "feedpipe" if "discord" in name.lower() else name


def build_payload(p: Pending) -> dict:
    body = p.summary or excerpt(p.md_path)
    meta = []
    if p.published:
        meta.append(f"🕒 {p.published[:16].replace('T', ' ')}")
    parts = [body, " ・ ".join(meta), p.url or ""]
    content = "\n\n".join(x for x in parts if x)
    if len(content) > MAX_CONTENT:
        keep = MAX_CONTENT - len(p.url or "") - 4
        content = body[:keep] + "…\n\n" + (p.url or "")
    payload: dict = {
        "thread_name": (p.title or "(無題)")[:MAX_THREAD_NAME],
        "username": _username(p.feed_title),
        "content": content,
        # 記事中の @everyone などで通知が飛ばないようにする
        "allowed_mentions": {"parse": []},
    }
    if tag := p.tags.get(str(p.feed_id)):
        payload["applied_tags"] = [tag]
    return payload


def notify_pending(
    conn: sqlite3.Connection,
    *,
    limit: int,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
    interval: float = SEND_INTERVAL,
) -> NotifyStats:
    stats = NotifyStats()
    items, stats.no_channel = pending_items(conn)
    stats.remaining = max(len(items) - limit, 0)
    for i, p in enumerate(items[:limit]):
        payload = build_payload(p)
        if dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
            stats.sent += 1
            continue
        if i:
            time.sleep(interval)
        try:
            msg = send_webhook(p.webhook_url, payload, transport=transport)
        except (DiscordError, httpx.HTTPError) as e:
            log.warning("通知に失敗: %s (%s)", p.title, e)
            with conn:
                db.update_item(conn, p.item_id, error=f"notify: {e}")
            stats.failed += 1
            stats.errors.append(f"{p.title}: {e}")
            continue
        with conn:
            db.update_item(
                conn,
                p.item_id,
                status="notified",
                notified_at=db.now_iso(),
                discord_thread_id=(msg or {}).get("channel_id"),
                error=None,
            )
        stats.sent += 1
    return stats
