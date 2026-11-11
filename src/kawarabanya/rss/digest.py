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

from kawarabanya.core import chat as chatdb
from kawarabanya.core.chat import DIGEST_KEY, Chat
from kawarabanya.core.llm import Summarizer, lead
from kawarabanya.rss import archive, prefs, trends
from kawarabanya.rss.genres import interest_of, top_genre

MAX_CONTENT = 2000
USERNAME = "瓦版屋 まとめ"
# 話題を選ばせる見出しの数（重要度の高い順）。150 件では話題が散り、見出しにない話を書くことが増えた
MAX_TREND_TITLES = 20


@dataclass
class DigestItem:
    """まとめに載せる 1 記事。"""
    title: str
    url: str | None  # 投稿へのリンク
    priority: float | None
    likes: int | None
    # 学んだ好み（rss/prefs.py）を除いた点。まとめの 1 枠は、これだけで選ぶ（好みに寄りすぎないため）
    base: float = 0.0


# 好き（like）の記事は重要度にこれを足し、低め（down）の記事は引いて並べる
LIKE_BONUS = 0.3


def collect(
    conn: sqlite3.Connection,
    platform: str,
    genre: str,
    since: datetime,
    levels: dict[str, str] | None = None,
    words: dict[str, int] | None = None,
    learned: prefs.Prefs | None = None,
    pref_max: float = 0.2,
    site_max: float = 0.1,
) -> list[DigestItem]:
    """since 以降に投稿したジャンルの記事を、重要度の高い順に返す（判定のないものは最後）。

    levels（genres.interest_levels）とサイトごとの interest（feeds option）で、うすい・ミュートの記事は除き、
    好きの記事は上に、低めの記事は下に寄せる。世間での話題になり方（rss/trends.py）でも上に寄せる:
    同じ出来事を報じたサイトの数と、words（Google トレンドの急上昇ワード → 検索数）に当たること。
    learned（👍・👎 から学んだ好み）があれば、その点も足す（DigestItem.base には含めない）。
    リンクは platform（discord・slack）の投稿へ。
    """
    levels = levels or {}
    sources = trends.source_counts(conn, since - timedelta(days=1))
    rows = conn.execute(
        "SELECT i.id, i.feed_id, i.title AS original, i.md_path, COALESCE(i.subgenre, f.category) AS category, COALESCE(i.title_ja, i.title) AS title,"
        " p.url AS post_url, i.priority, i.likes,"
        " json_extract(f.options, '$.interest') AS feed_interest FROM items i"
        " JOIN feeds f ON f.id = i.feed_id LEFT JOIN posts p ON p.item_id = i.id AND p.platform = ?"
        " WHERE i.status = 'notified' AND i.notified_at >= ?",
        (platform, since.isoformat(timespec="seconds")),
    ).fetchall()
    scored = []
    for r in rows:
        level = interest_of(levels, r["category"], r["feed_interest"])
        if top_genre(r["category"]) != genre or level in ("low", "mute"):
            continue
        bonus = {"like": LIKE_BONUS, "down": -LIKE_BONUS}.get(level, 0)
        score = (r["priority"] if r["priority"] is not None else -1) + bonus + trends.source_bonus(sources.get(r["id"], 1))
        body = archive.read_body(r["md_path"]) if r["md_path"] and Path(r["md_path"]).exists() else ""
        if words:
            score += trends.trend_bonus(f"{r['title'] or ''}\n{lead(body or '')}", words)
        liked = 0.0
        if learned:
            feats = prefs.features(r["category"], r["original"], body)
            liked = prefs.bonus(learned, r["feed_id"], feats, pref_max, site_max)
        item = DigestItem(r["title"] or "(無題)", r["post_url"], r["priority"], r["likes"], score)
        scored.append((score + liked, item))
    return [it for _, it in sorted(scored, key=lambda x: -x[0])]


def explore(items: list[DigestItem], top: int) -> list[DigestItem]:
    """上位 top 件の最後の 1 枠を、好みを除いた点（base）が最も高い残りの記事にする（好みの外の記事も目に入るように）。"""
    if top < 2 or len(items) <= top - 1:
        return items
    rest = items[top - 1:]
    pick = max(rest, key=lambda i: i.base)
    return [*items[: top - 1], pick, *(i for i in rest if i is not pick)]


def _link_text(title: str) -> str:
    # Discord の [文字](URL) の中で使えない角括弧を全角にする
    return title.replace("[", "［").replace("]", "］")


def render(trend: list[str], items: list[DigestItem], top: int, limit: int = MAX_CONTENT) -> str:
    """まとめの本文（話題＋重要度順の投稿へのリンク＋「ほか N 件」）を limit 字に収めて作る。"""
    topics = "".join(f"- {t}\n" for t in trend)
    lines = [f"**話題**\n{topics}\n**注目の記事**" if trend else "**注目の記事**"]
    for it in items[:top]:
        line = f"- [{_link_text(it.title)}]({it.url})" if it.url else f"- {it.title}"
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
    chat: Chat,
    summarizer: Summarizer | None,
    genre: str,
    *,
    label: str | None = None,
    levels: dict[str, str] | None = None,
    now: datetime,
    top: int,
    window_hours: int,
    due_after: datetime | None = None,
    learned: prefs.Prefs | None = None,
    pref_max: float = 0.2,
    site_max: float = 0.1,
    update: bool = False,
    dry_run: bool = False,
    trend_cache: dict | None = None,
) -> str | None:
    """genre のまとめ（前回のまとめ以降の記事）を投稿し、本文を返す。見出しには label（なければ genre）を使う。

    due_after があれば、その時刻より後にまとめを投稿済みなら何もしない（定時の投稿用）。
    update=True なら、最後に投稿したまとめを作り直して書き換える。
    投稿する記事がない・送り先がない・（更新なら）未投稿のときは None。
    """
    channel = chatdb.channel(conn, chat.platform, DIGEST_KEY)
    last, before = [*_history(conn, chat.platform, genre), None, None][:2]
    if channel is None or (update and last is None):
        return None
    if not update and due_after and last and datetime.fromisoformat(last["created_at"]) >= due_after:
        return None
    # 前回のまとめ以降（更新なら、その前のまとめ以降）の記事。ただし window_hours 時間まで
    prev = before if update else last
    since = now - timedelta(hours=window_hours)
    if prev:
        since = max(since, datetime.fromisoformat(prev["created_at"]))
    words = trends.load(conn, now.astimezone(UTC))
    items = explore(collect(conn, chat.platform, genre, since, levels, words, learned, pref_max, site_max), top)
    if not items:
        return None
    trend = []
    titles = tuple(i.title for i in items[:MAX_TREND_TITLES])
    if trend_cache is not None and titles in trend_cache:
        trend = trend_cache[titles]
    elif summarizer:
        trend = await summarizer.trend(list(titles))
        if trend_cache is not None:
            trend_cache[titles] = trend
    header = f"## 📰 {label or genre} {now.astimezone():%m/%d %H:%M} のまとめ（{len(items)} 件）"
    content = header + "\n" + render(trend, items, top, limit=MAX_CONTENT - len(header) - 1)
    if dry_run:
        return content
    if update:
        chat.edit(channel, last["ref"], content)
        return content
    # まとめはテキストチャンネル（#ニュース）に、ジャンルごとに 1 メッセージで投稿する
    ref = chat.send(channel, content, username=USERNAME)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO digests (platform, genre, day, ref, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat.platform, genre, now.astimezone().strftime("%Y-%m-%d %H:%M:%S"), ref, now.isoformat(timespec="seconds")),
        )
    return content


def announce(conn: sqlite3.Connection, chat: Chat, labels: list[str]) -> None:
    """まとめを投稿し終えたことを、メンション付きで 1 通だけ知らせる（ジャンルごとに鳴らさない）。"""
    channel = chatdb.channel(conn, chat.platform, DIGEST_KEY)
    if channel and labels and chat.mention_user_id:
        chat.send(channel, f"📰 まとめを投稿しました: {'、'.join(labels)}", username=USERNAME, mention=True)


def delete_digest(conn: sqlite3.Connection, chat: Chat, genre: str, day: str) -> bool:
    """genre のまとめ（digests.day が day のもの）を消す。投稿済みの記録も消す。"""
    row = conn.execute(
        "SELECT ref FROM digests WHERE platform = ? AND genre = ? AND day = ?", (chat.platform, genre, day)
    ).fetchone()
    if row is None:
        return False
    if (channel := chatdb.channel(conn, chat.platform, DIGEST_KEY)) is not None and row[0]:
        chat.remove(channel, row[0])
    with conn:
        conn.execute("DELETE FROM digests WHERE platform = ? AND genre = ? AND day = ?", (chat.platform, genre, day))
    return True


def _history(conn: sqlite3.Connection, platform: str, genre: str) -> list[sqlite3.Row]:
    """genre の投稿済みのまとめ（新しい順）。"""
    return conn.execute(
        "SELECT day, ref, created_at FROM digests WHERE platform = ? AND genre = ? ORDER BY created_at DESC",
        (platform, genre),
    ).fetchall()



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
