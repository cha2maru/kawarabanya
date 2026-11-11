"""チャット（Discord・Slack）への投稿の共通の形。

RSS・予定の機能はこの Chat だけを使い、どのサービスに投稿するかを気にしない。サービスごとの作りは
core/discord_api.py（DiscordChat）と core/slack_api.py（SlackChat）。送り先（channels）と投稿（posts）は
サービスごとに DB に置き、[chat] targets のすべてに同じ記事を投稿する。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from kawarabanya.core import db

PLATFORMS = ("discord", "slack")
# まとめ・予定の送り先（channels.key）。ジャンルの送り先と区別するため "#" で始める
DIGEST_KEY = "#digest"
SCHEDULE_KEY = "#schedule"
# ジャンルのないフィードの送り先
UNCATEGORIZED = "uncategorized"
LIKE = "like"
DISLIKE = "dislike"


class ChatError(Exception):
    """チャットの API が失敗したときの例外。"""


class PostGoneError(ChatError):
    """投稿がもう消えている。"""


@dataclass
class Channel:
    """送り先（ジャンルのフォーラム・チャンネル、まとめ・予定のチャンネル）。"""
    platform: str
    key: str  # ジャンル（"tech"）か DIGEST_KEY・SCHEDULE_KEY
    channel_id: str
    name: str
    webhook_url: str | None = None  # Discord だけ
    guild_id: str | None = None  # Discord のサーバー・Slack のワークスペース
    # "sub:<サブジャンル>" → Discord のタグ ID・Slack の本文の先頭に書くラベル
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class Article:
    """投稿する 1 記事（サービスごとの形にするのは Chat）。"""
    title: str
    feed_title: str | None
    body: str  # 要約か本文の先頭
    url: str | None
    published: str | None  # ISO 8601
    subgenre: str  # タグ・ラベルを選ぶサブジャンル（なければジャンル）


@dataclass
class Posted:
    """投稿した結果。ref は投稿の ID（Discord はスレッド ID、Slack は "チャンネル/ts"）。"""
    ref: str
    url: str | None


class Chat(Protocol):
    """1 つのチャットサービスへの投稿。send の content は Discord の Markdown で書く（Slack 版が書き換える）。"""
    platform: str
    mention_user_id: str  # まとめ・予定でメンションする自分の ID（空ならしない）

    def post_article(self, ch: Channel, a: Article) -> Posted:
        """記事を投稿する。"""

    def retag(self, ch: Channel, ref: str, a: Article) -> None:
        """投稿のサブジャンル（タグ・ラベル）を a.subgenre に変える。投稿が消えていれば PostGoneError。"""

    def delete_post(self, ref: str) -> None:
        """投稿を消す。すでに消えていれば何もしない。"""

    def reply(self, ref: str, text: str) -> int:
        """投稿のスレッドに返信する。送った通数を返す。"""

    def reactions(self, ref: str) -> tuple[int, int] | None:
        """投稿の (👍, 👎) の数。投稿が消えていれば None。"""

    def send(self, ch: Channel, content: str, *, username: str, mention: bool = False) -> str:
        """まとめ・予定・お知らせのメッセージを送り、その ID を返す。mention なら mention_user_id に通知する。"""

    def edit(self, ch: Channel, ref: str, content: str) -> None:
        """send で送ったメッセージを書き換える。"""

    def remove(self, ch: Channel, ref: str) -> None:
        """send で送ったメッセージを消す。すでに消えていれば何もしない。"""


def article_text(a: Article, limit: int) -> str:
    """記事の本文（要約・公開日時・URL）を limit 字に収める。長ければ本文を切り、URL は残す。"""
    meta = []
    if a.published:
        local = datetime.fromisoformat(a.published).astimezone()
        meta.append(f"🕒 {local:%Y-%m-%d %H:%M}")
    content = "\n\n".join(x for x in [a.body, " ・ ".join(meta), a.url or ""] if x)
    if len(content) > limit:
        keep = limit - len(a.url or "") - 4
        content = a.body[:keep] + "…\n\n" + (a.url or "")
    return content


def channels(conn: sqlite3.Connection, platform: str | None = None, key: str | None = None) -> list[Channel]:
    """登録済みの送り先。platform・key で絞る。"""
    rows = conn.execute(
        "SELECT * FROM channels WHERE (? IS NULL OR platform = ?) AND (? IS NULL OR key = ?) ORDER BY platform, key",
        (platform, platform, key, key),
    ).fetchall()
    return [
        Channel(r["platform"], r["key"], r["channel_id"], r["name"], r["webhook_url"], r["guild_id"],
                json.loads(r["tags"]) if r["tags"] else {})
        for r in rows
    ]


def channel(conn: sqlite3.Connection, platform: str, key: str) -> Channel | None:
    """1 つの送り先（なければ None）。"""
    found = channels(conn, platform, key)
    return found[0] if found else None


def save_channel(conn: sqlite3.Connection, ch: Channel) -> None:
    """送り先を登録する（同じサービス・キーは上書き）。"""
    with conn:
        conn.execute(
            "INSERT INTO channels (platform, key, channel_id, name, webhook_url, guild_id, tags, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(platform, key) DO UPDATE SET"
            " channel_id = excluded.channel_id, name = excluded.name, webhook_url = excluded.webhook_url,"
            " guild_id = excluded.guild_id, tags = excluded.tags",
            (ch.platform, ch.key, ch.channel_id, ch.name, ch.webhook_url, ch.guild_id, json.dumps(ch.tags),
             db.now_iso()),
        )


def record_post(conn: sqlite3.Connection, item_id: int, platform: str, posted: Posted) -> None:
    """記事を投稿したことを記録する（同じサービスに投稿し直したら置き換え、👍・👎 は 0 から）。"""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO posts (item_id, platform, ref, url, likes, dislikes, created_at)"
            " VALUES (?, ?, ?, ?, 0, 0, ?)",
            (item_id, platform, posted.ref, posted.url, db.now_iso()),
        )
        sum_reactions(conn, item_id)


def sum_reactions(conn: sqlite3.Connection, item_id: int) -> None:
    """記事の 👍・👎（items.likes・dislikes）を、サービスごとの投稿の数の合計にする（好みとまとめはこれを使う）。"""
    conn.execute(
        "UPDATE items SET likes = (SELECT SUM(likes) FROM posts WHERE item_id = ?),"
        " dislikes = (SELECT SUM(dislikes) FROM posts WHERE item_id = ?) WHERE id = ?",
        (item_id, item_id, item_id),
    )


def count_reaction(conn: sqlite3.Connection, platform: str, ref: str, kind: str, delta: int) -> bool:
    """投稿に付いた・外れた 👍（LIKE）・👎（DISLIKE）を、その場で数に足す（Bot がイベントで受ける）。記事の投稿なら True。"""
    col = {LIKE: "likes", DISLIKE: "dislikes"}.get(kind)
    row = conn.execute("SELECT item_id FROM posts WHERE platform = ? AND ref = ?", (platform, ref)).fetchone()
    if col is None or row is None:
        return False
    with conn:
        conn.execute(
            f"UPDATE posts SET {col} = MAX(COALESCE({col}, 0) + ?, 0) WHERE platform = ? AND ref = ?",
            (delta, platform, ref),
        )
        sum_reactions(conn, row[0])
    return True


def find_item(conn: sqlite3.Connection, platform: str, ref: str) -> int | None:
    """投稿の ID から記事の ID を探す（なければ None）。"""
    row = conn.execute("SELECT item_id FROM posts WHERE platform = ? AND ref = ?", (platform, ref)).fetchone()
    return row[0] if row else None
