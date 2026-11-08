"""ジャンルごとのまとめを、まとめ用のテキストチャンネル（既定 #ニュース）に投稿する。

投稿するのは [digest] times の時刻（既定 6:30 と 18:30）と、`kawarabanya digest` を実行したとき。
どちらも前回のまとめ以降（最大 window_hours 時間）の記事をまとめる。

まとめ = LLM が見出しの一覧から選ぶ「話題」（最大 3 つ）＋ 重要度の高い順の投稿へのリンク。
リンクの一覧は LLM に書かせない（関係のない話題を 1 行に混ぜることがあったため）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import httpx

from kawarabanya.core.discord_api import (
    DIGEST_KEY,
    delete_webhook_message,
    edit_webhook_message,
    post_url,
    send_webhook,
)
from kawarabanya.core.llm import Summarizer, lead
from kawarabanya.rss import archive, trends
from kawarabanya.rss.genres import interest_of, top_genre

MAX_CONTENT = 2000
# 話題を選ばせる見出しの数（重要度の高い順）。150 件では話題が散り、見出しにない話を書くことが増えた
MAX_TREND_TITLES = 20


@dataclass
class DigestItem:
    """まとめに載せる 1 記事。"""
    title: str
    thread_id: str | None
    priority: float | None
    likes: int | None


# 好き（like）の記事は重要度にこれを足し、低め（down）の記事は引いて並べる
LIKE_BONUS = 0.3


def collect(
    conn: sqlite3.Connection,
    genre: str,
    since: datetime,
    levels: dict[str, str] | None = None,
    words: dict[str, int] | None = None,
) -> list[DigestItem]:
    """since 以降に投稿したジャンルの記事を、重要度の高い順に返す（判定のないものは最後）。

    levels（genres.interest_levels）とサイトごとの interest（feeds option）で、うすい・ミュートの記事は除き、
    好きの記事は上に、低めの記事は下に寄せる。世間での話題になり方（rss/trends.py）でも上に寄せる:
    同じ出来事を報じたサイトの数と、words（Google トレンドの急上昇ワード → 検索数）に当たること。
    """
    levels = levels or {}
    sources = trends.source_counts(conn, since - timedelta(days=1))
    rows = conn.execute(
        "SELECT i.id, i.md_path, COALESCE(i.subgenre, f.category) AS category, i.title, i.discord_thread_id, i.priority, i.likes,"
        " json_extract(f.options, '$.interest') AS feed_interest FROM items i"
        " JOIN feeds f ON f.id = i.feed_id WHERE i.status = 'notified' AND i.notified_at >= ?",
        (since.isoformat(timespec="seconds"),),
    ).fetchall()
    scored = []
    for r in rows:
        level = interest_of(levels, r["category"], r["feed_interest"])
        if top_genre(r["category"]) != genre or level in ("low", "mute"):
            continue
        bonus = {"like": LIKE_BONUS, "down": -LIKE_BONUS}.get(level, 0)
        score = (r["priority"] if r["priority"] is not None else -1) + bonus + trends.source_bonus(sources.get(r["id"], 1))
        if words:
            body = archive.read_body(r["md_path"]) if r["md_path"] and Path(r["md_path"]).exists() else ""
            score += trends.trend_bonus(f"{r['title'] or ''}\n{lead(body or '')}", words)
        scored.append((score, DigestItem(r["title"] or "(無題)", r["discord_thread_id"], r["priority"], r["likes"])))
    return [it for _, it in sorted(scored, key=lambda x: -x[0])]


def _link_text(title: str) -> str:
    # Discord の [文字](URL) の中で使えない角括弧を全角にする
    return title.replace("[", "［").replace("]", "］")


def render(trend: list[str], items: list[DigestItem], guild_id: str, top: int, limit: int = MAX_CONTENT) -> str:
    """まとめの本文（話題＋重要度順の投稿へのリンク＋「ほか N 件」）を limit 字に収めて作る。"""
    topics = "".join(f"- {t}\n" for t in trend)
    lines = [f"**話題**\n{topics}\n**注目の記事**" if trend else "**注目の記事**"]
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
    levels: dict[str, str] | None = None,
    now: datetime,
    top: int,
    window_hours: int,
    due_after: datetime | None = None,
    update: bool = False,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """genre のまとめ（前回のまとめ以降の記事）を投稿し、本文を返す。見出しには label（なければ genre）を使う。

    due_after があれば、その時刻より後にまとめを投稿済みなら何もしない（定時の投稿用）。
    update=True なら、最後に投稿したまとめを作り直して書き換える。
    投稿する記事がない・送り先がない・（更新なら）未投稿のときは None。
    """
    channel = digest_channel(conn)
    last, before = [*_history(conn, genre), None, None][:2]
    if channel is None or (update and last is None):
        return None
    if not update and due_after and last and datetime.fromisoformat(last["created_at"]) >= due_after:
        return None
    # 前回のまとめ以降（更新なら、その前のまとめ以降）の記事。ただし window_hours 時間まで
    prev = before if update else last
    since = now - timedelta(hours=window_hours)
    if prev:
        since = max(since, datetime.fromisoformat(prev["created_at"]))
    items = collect(conn, genre, since, levels, trends.load(conn, now.astimezone(UTC)))
    if not items:
        return None
    trend = []
    if summarizer:
        trend = await summarizer.trend([i.title for i in items[:MAX_TREND_TITLES]])
    header = f"## 📰 {label or genre} {now.astimezone():%m/%d %H:%M} のまとめ（{len(items)} 件）"
    content = header + "\n" + render(trend, items, channel["guild_id"], top, limit=MAX_CONTENT - len(header) - 1)
    if dry_run:
        return content
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    if update:
        edit_webhook_message(channel["webhook_url"], last["thread_id"], payload, transport=transport)
        return content
    # まとめはテキストチャンネル（#ニュース）に、ジャンルごとに 1 メッセージで投稿する
    msg = send_webhook(channel["webhook_url"], {"username": "瓦版屋 まとめ", **payload}, transport=transport)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO digests (genre, day, thread_id, created_at) VALUES (?, ?, ?, ?)",
            (genre, now.astimezone().strftime("%Y-%m-%d %H:%M:%S"), msg.get("id"), now.isoformat(timespec="seconds")),
        )
    return content


def delete_digest(
    conn: sqlite3.Connection, genre: str, day: str, *, transport: httpx.BaseTransport | None = None
) -> bool:
    """genre のまとめ（digests.day が day のもの）を消す。投稿済みの記録も消す。"""
    channel = digest_channel(conn)
    message_id = _posted(conn, genre, day)
    if message_id is None:
        return False
    if channel is not None:
        delete_webhook_message(channel["webhook_url"], message_id, transport=transport)
    with conn:
        conn.execute("DELETE FROM digests WHERE genre = ? AND day = ?", (genre, day))
    return True


def _history(conn: sqlite3.Connection, genre: str) -> list[sqlite3.Row]:
    """genre の投稿済みのまとめ（新しい順）。"""
    return conn.execute(
        "SELECT day, thread_id, created_at FROM digests WHERE genre = ? ORDER BY created_at DESC", (genre,)
    ).fetchall()


def digest_channel(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """まとめの投稿先（Webhook とサーバー）。未登録なら None。"""
    return conn.execute(
        "SELECT webhook_url, guild_id FROM discord_channels WHERE category = ?", (DIGEST_KEY,)
    ).fetchone()


def _posted(conn: sqlite3.Connection, genre: str, day: str) -> str | None:
    """投稿済みのまとめのメッセージ ID（未投稿なら None）。"""
    row = conn.execute("SELECT thread_id FROM digests WHERE genre = ? AND day = ?", (genre, day)).fetchone()
    return row[0] if row else None


def _slots(now: datetime, times: list[str], days: tuple[int, ...]) -> list[datetime]:
    local = now.astimezone()
    return [
        datetime.combine(local.date() + timedelta(days=d), time(*map(int, t.split(":"))), local.tzinfo)
        for d in days
        for t in times
    ]


def last_slot(now: datetime, times: list[str]) -> datetime | None:
    """now 以前で最も新しい投稿時刻（times は "HH:MM"、ローカル時刻）。times が空なら None。"""
    past = [s for s in _slots(now, times, (-1, 0)) if s <= now]
    return max(past) if past else None


def next_slot(now: datetime, times: list[str]) -> datetime | None:
    """now より後で最も早い投稿時刻。times が空なら None。"""
    future = [s for s in _slots(now, times, (0, 1)) if s > now]
    return min(future) if future else None
