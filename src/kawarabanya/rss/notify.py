"""保存済みの記事を Discord のフォーラムに投稿する（1 記事 = 1 投稿）。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from kawarabanya.core import db
from kawarabanya.core.discord_api import (
    UNCATEGORIZED,
    DiscordClient,
    DiscordError,
    count_likes,
    post_url,
    send_webhook,
)
from kawarabanya.core.llm import Summarizer
from kawarabanya.rss import archive
from kawarabanya.rss.genres import Genre, interest_of
from kawarabanya.rss.item import is_recent
from kawarabanya.rss.pipeline import classify_item

log = logging.getLogger(__name__)

MAX_THREAD_NAME = 100
MAX_CONTENT = 2000
MAX_USERNAME = 80
EXCERPT_CHARS = 200
# Webhook のレート制限（5 件 / 2 秒）に余裕を持たせる間隔
SEND_INTERVAL = 0.5


# 記事のカテゴリ（LLM が判定したサブジャンル、なければフィードのカテゴリ）と、そのトップレベルのジャンル
CAT = "COALESCE(i.subgenre, f.category)"
TOP = f"CASE WHEN instr({CAT}, '/') > 0 THEN substr({CAT}, 1, instr({CAT}, '/') - 1) ELSE {CAT} END"


@dataclass
class Pending:
    """投稿待ちの 1 記事と、その送り先。"""
    item_id: int
    feed_id: int
    feed_title: str | None
    category: str | None
    subgenre: str | None
    title: str | None
    url: str | None
    published: str | None
    fetched_at: str
    summary: str | None
    md_path: str | None
    webhook_url: str
    guild_id: str | None
    tags: dict[str, str]


@dataclass
class NotifyStats:
    """notify_pending の結果の件数。"""
    sent: int = 0
    failed: int = 0
    no_channel: int = 0  # 送り先のチャンネルが未登録
    remaining: int = 0  # 上限に達したため次回に回したもの
    stale: int = 0  # 公開から日数が経っていたため投稿せずに済ませたもの
    muted: int = 0  # ミュート（interest = "mute"）のジャンルなので投稿しなかったもの
    errors: list[str] = field(default_factory=list)


def pending_items(conn: sqlite3.Connection, category: str | None = None) -> tuple[list[Pending], int]:
    """通知待ちの記事と、送り先がないため送れない記事の数を返す。"""
    rows = conn.execute(
        "SELECT i.id, i.feed_id, f.title AS feed_title, f.category, i.subgenre, i.title, i.url, i.published, i.fetched_at, i.summary,"
        " i.md_path, d.webhook_url, d.guild_id, d.tags"
        " FROM items i JOIN feeds f ON f.id = i.feed_id"
        # フォーラムはトップレベルのジャンル（"news/world" なら "news"）ごと
        " LEFT JOIN discord_channels d ON d.category = COALESCE(" + TOP + ", ?)"
        " WHERE i.status = 'saved' AND (? IS NULL OR COALESCE(" + TOP + ", ?) = ?)"
        " ORDER BY i.published, i.id",
        (UNCATEGORIZED, category, UNCATEGORIZED, category),
    ).fetchall()
    ready = [
        Pending(
            item_id=r["id"],
            feed_id=r["feed_id"],
            feed_title=r["feed_title"],
            category=r["category"],
            subgenre=r["subgenre"],
            title=r["title"],
            url=r["url"],
            published=r["published"],
            fetched_at=r["fetched_at"],
            summary=r["summary"],
            md_path=r["md_path"],
            webhook_url=r["webhook_url"],
            guild_id=r["guild_id"],
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
    text = archive.read_body(md_path)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # 画像
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # リンクは文字だけ
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    plain = re.sub(r"(?<!\\)[*_`>]+", "", " ".join(lines))  # 書式記号（エスケープされたものは残す）
    plain = re.sub(r"\\([\\`*_{}\[\]()#+\-.!~|>])", r"\1", plain)  # Markdown のエスケープを外す
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain if len(plain) <= limit else plain[:limit].rstrip() + "…"


def _username(feed_title: str | None) -> str:
    # Webhook の表示名に "discord" は使えない
    """投稿者として表示する名前（フィード名）。Discord の制限で「discord」を含む名前は使えない。"""
    name = (feed_title or "kawarabanya").strip()[:MAX_USERNAME]
    return "kawarabanya" if "discord" in name.lower() else name


def build_payload(p: Pending) -> dict:
    """記事 1 件をフォーラムに投稿する Webhook の内容（スレッド名・本文・タグ）を作る。"""
    body = p.summary or excerpt(p.md_path)
    meta = []
    if p.published:
        local = datetime.fromisoformat(p.published).astimezone()
        meta.append(f"🕒 {local:%Y-%m-%d %H:%M}")
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
    # タグはサブジャンル（LLM が判定した記事のサブジャンル、なければフィードのサブジャンル）
    tags = [p.tags.get(f"sub:{p.subgenre or p.category}")]
    if tags := [t for t in tags if t]:
        payload["applied_tags"] = tags
    return payload


def notify_pending(
    conn: sqlite3.Connection,
    *,
    limit: int,
    max_age_days: int,
    category: str | None = None,
    levels: dict[str, str] | None = None,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
    interval: float = SEND_INTERVAL,
) -> NotifyStats:
    """保存済み・未投稿の記事を limit 件まで投稿する。

    公開から max_age_days 日を過ぎたものは投稿せず stale に、ミュートのジャンル（levels）の記事は muted にする。投稿後は Markdown に投稿へのリンクを書き込む。
    """
    stats = NotifyStats()
    items, stats.no_channel = pending_items(conn, category)
    old = [p for p in items if not is_recent(p.published, p.fetched_at, max_age_days)]
    items = [p for p in items if p not in old]
    stats.stale = len(old)
    muted = [p for p in items if interest_of(levels or {}, p.subgenre or p.category) == "mute"]
    items = [p for p in items if p not in muted]
    stats.muted = len(muted)
    if not dry_run:
        with conn:
            for p in old:
                db.update_item(conn, p.item_id, status="stale")
            for p in muted:
                db.update_item(conn, p.item_id, status="muted")
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
        thread_id = (msg or {}).get("channel_id")
        with conn:
            db.update_item(
                conn, p.item_id, status="notified", notified_at=db.now_iso(), discord_thread_id=thread_id, error=None
            )
        if thread_id and p.guild_id and p.md_path and Path(p.md_path).exists():
            # 保存した記事から Discord の投稿に飛べるようにする
            archive.set_field(p.md_path, "discord", post_url(p.guild_id, thread_id))
        stats.sent += 1
    return stats


def sync_likes(
    conn: sqlite3.Connection,
    client: DiscordClient,
    *,
    within_days: int = 7,
    interval_hours: float = 6,
) -> int:
    """投稿から within_days 日以内の記事の 👍 の数を記録する。確認した件数を返す。

    同じ投稿は interval_hours 時間に 1 回だけ確認する（毎回全件を読みに行かない）。
    """
    now = datetime.now(UTC)
    rows = conn.execute(
        "SELECT id, discord_thread_id, likes_checked_at FROM items"
        " WHERE status = 'notified' AND discord_thread_id IS NOT NULL AND notified_at >= ?",
        ((now - timedelta(days=within_days)).isoformat(timespec="seconds"),),
    ).fetchall()
    due = [
        r for r in rows
        if not r["likes_checked_at"]
        or datetime.fromisoformat(r["likes_checked_at"]) <= now - timedelta(hours=interval_hours)
    ]
    for r in due:
        likes = count_likes(client, r["discord_thread_id"])
        with conn:
            db.update_item(conn, r["id"], likes=likes, likes_checked_at=db.now_iso())
    return len(due)


async def retag_posts(
    conn: sqlite3.Connection,
    client: DiscordClient,
    summarizer: Summarizer | None,
    genres: dict[str, Genre] | None,
    item_ids: list[int] | None = None,
) -> tuple[int, int]:
    """投稿済みの記事のタグを、サブジャンルに付け直す。(書き換えた投稿数, 確認した投稿数) を返す。

    サブジャンルが未判定の記事は（summarizer があれば）LLM で判定してから付ける。item_ids があればその記事だけ。
    """
    rows = conn.execute(
        "SELECT i.id, i.title, i.md_path, i.subgenre, i.discord_thread_id, f.category, d.tags"
        " FROM items i JOIN feeds f ON f.id = i.feed_id"
        " LEFT JOIN discord_channels d ON d.category = COALESCE(" + TOP + ", ?)"
        " WHERE i.status = 'notified' AND i.discord_thread_id IS NOT NULL",
        (UNCATEGORIZED,),
    ).fetchall()
    if item_ids is not None:
        rows = [r for r in rows if r["id"] in set(item_ids)]
    changed = 0
    for r in rows:
        subgenre = r["subgenre"]
        if subgenre is None and summarizer and r["md_path"] and Path(r["md_path"]).exists():
            body = archive.read_body(r["md_path"])
            category = await classify_item(
                conn, summarizer, genres, r["id"], r["title"], r["category"], body, allow_move=False
            )
            subgenre = category if category != r["category"] else None
        tags = json.loads(r["tags"]) if r["tags"] else {}
        wanted = [t for t in [tags.get(f"sub:{subgenre or r['category']}")] if t]
        try:
            client.request("PATCH", f"/channels/{r['discord_thread_id']}", {"applied_tags": wanted})
        except DiscordError as e:
            if "HTTP 404" in str(e):  # 投稿が消えている
                continue
            if "HTTP 403" in str(e):
                raise DiscordError(f"{e}（Bot に「スレッドの管理」の権限が必要です）") from None
            raise
        changed += 1
    return changed, len(rows)


def delete_duplicate_posts(conn: sqlite3.Connection, client: DiscordClient, *, apply: bool = False) -> list[sqlite3.Row]:
    """重複（duplicate）と判定した記事のフォーラムの投稿を返し、apply=True なら消す。"""
    rows = conn.execute(
        "SELECT id, title, discord_thread_id FROM items WHERE status = 'duplicate' AND discord_thread_id IS NOT NULL"
    ).fetchall()
    if not apply:
        return rows
    for r in rows:
        try:
            client.request("DELETE", f"/channels/{r['discord_thread_id']}")
        except DiscordError as e:
            if "HTTP 403" in str(e):
                raise DiscordError(f"{e}（Bot に「スレッドの管理」の権限が必要です）") from None
            if "HTTP 404" not in str(e):  # 404 はすでに消えている
                raise
        with conn:
            db.update_item(conn, r["id"], discord_thread_id=None)
    return rows
