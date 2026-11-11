"""保存済みの記事を、[chat] targets のチャット（Discord のフォーラム・Slack のチャンネル）に投稿する（1 記事 = 1 投稿）。"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from kawarabanya.core import chat as chatdb
from kawarabanya.core import db
from kawarabanya.core.chat import UNCATEGORIZED, Article, Channel, Chat, ChatError, PostGoneError
from kawarabanya.core.llm import Summarizer, is_foreign, plain, split_paragraphs
from kawarabanya.rss import archive
from kawarabanya.rss.genres import Genre, interest_of, top_genre
from kawarabanya.rss.item import is_recent
from kawarabanya.rss.pipeline import classify_item

log = logging.getLogger(__name__)

EXCERPT_CHARS = 200
# Webhook のレート制限（5 件 / 2 秒）・Slack の chat.postMessage（1 チャンネル 1 秒 1 件）に余裕を持たせる間隔
SEND_INTERVAL = 1.0


@dataclass
class Pending:
    """投稿待ちの 1 記事と、サービスごとの送り先。"""
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
    channels: dict[str, Channel]  # サービス → ジャンルの送り先
    title_ja: str | None = None  # 日本語以外の記事の見出しの訳

    def article(self) -> Article:
        """投稿する形にする。訳した見出しには原題も添える（4B のモデルは固有名詞を訳し違えることがある）。"""
        title = f"{self.title_ja}（{self.title}）" if self.title_ja else self.title or "(無題)"
        return Article(title, self.feed_title, self.summary or excerpt(self.md_path), self.url, self.published,
                       self.subgenre or self.category or UNCATEGORIZED)


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


def genre_channels(conn: sqlite3.Connection, platforms: list[str]) -> dict[str, dict[str, Channel]]:
    """ジャンル → サービス → 送り先。"""
    out: dict[str, dict[str, Channel]] = {}
    for ch in chatdb.channels(conn):
        if ch.platform in platforms and not ch.key.startswith("#"):
            out.setdefault(ch.key, {})[ch.platform] = ch
    return out


def pending_items(
    conn: sqlite3.Connection, platforms: list[str], category: str | None = None
) -> tuple[list[Pending], int]:
    """通知待ちの記事と、送り先がない（どのサービスにもジャンルの送り先がない）ため送れない記事の数を返す。"""
    rows = conn.execute(
        "SELECT i.id, i.feed_id, f.title AS feed_title, f.category, i.subgenre, i.title, i.title_ja, i.url,"
        " i.published, i.fetched_at, i.summary, i.md_path FROM items i JOIN feeds f ON f.id = i.feed_id"
        " WHERE i.status = 'saved' ORDER BY i.published, i.id"
    ).fetchall()
    by_genre = genre_channels(conn, platforms)
    ready, missing = [], 0
    for r in rows:
        # 送り先はトップレベルのジャンル（"news/world" なら "news"）ごと
        top = top_genre(r["subgenre"] or r["category"]) or UNCATEGORIZED
        if category and top != category:
            continue
        if not (chs := by_genre.get(top)):
            missing += 1
            continue
        ready.append(Pending(
            item_id=r["id"], feed_id=r["feed_id"], feed_title=r["feed_title"], category=r["category"],
            subgenre=r["subgenre"], title=r["title"], url=r["url"], published=r["published"],
            fetched_at=r["fetched_at"], summary=r["summary"], md_path=r["md_path"], channels=chs,
            title_ja=r["title_ja"],
        ))
    return ready, missing


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


def notify_pending(
    conn: sqlite3.Connection,
    chats: list[Chat],
    *,
    limit: int,
    max_age_days: int,
    category: str | None = None,
    levels: dict[str, str] | None = None,
    dry_run: bool = False,
    interval: float = SEND_INTERVAL,
) -> NotifyStats:
    """保存済み・未投稿の記事を limit 件まで、chats のすべてに投稿する。

    公開から max_age_days 日を過ぎたものは投稿せず stale に、ミュートのジャンル（levels）の記事は muted にする。
    1 つのサービスにでも投稿できれば notified にし、Markdown にサービスごとの投稿へのリンクを書き込む。
    """
    stats = NotifyStats()
    items, stats.no_channel = pending_items(conn, [c.platform for c in chats], category)
    old = [p for p in items if not is_recent(p.published, p.fetched_at, max_age_days)]
    items = [p for p in items if p not in old]
    stats.stale = len(old)
    feed_levels = dict(conn.execute("SELECT id, json_extract(options, '$.interest') FROM feeds").fetchall())
    muted = [p for p in items if interest_of(levels or {}, p.subgenre or p.category, feed_levels.get(p.feed_id)) == "mute"]
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
        a = p.article()
        if dry_run:
            print(f"[{', '.join(p.channels)}] {a.title}\n{a.body[:100]}")
            stats.sent += 1
            continue
        if i:
            time.sleep(interval)
        errors = []
        posted = 0
        for c in chats:
            if (ch := p.channels.get(c.platform)) is None:
                continue
            try:
                result = c.post_article(ch, a)
            except (ChatError, httpx.HTTPError) as e:
                log.warning("%s への通知に失敗: %s (%s)", c.platform, p.title, e)
                errors.append(f"{c.platform}: {e}")
                continue
            chatdb.record_post(conn, p.item_id, c.platform, result)
            posted += 1
            if result.url and p.md_path and Path(p.md_path).exists():
                # 保存した記事から投稿に飛べるようにする
                archive.set_field(p.md_path, c.platform, result.url)
        # shortcut: 一部のサービスだけ失敗した記事は、そのサービスに投稿し直さない。失敗が続くようなら送り直しを作る
        with conn:
            if posted:
                db.update_item(conn, p.item_id, status="notified", notified_at=db.now_iso(),
                               error="; ".join(errors) or None)
            else:
                db.update_item(conn, p.item_id, error="notify: " + "; ".join(errors))
        if posted:
            stats.sent += 1
        else:
            stats.failed += 1
            stats.errors.append(f"{p.title}: {'; '.join(errors)}")
    return stats


def sync_likes(
    conn: sqlite3.Connection,
    chats: list[Chat],
    *,
    within_days: int = 7,
    interval_hours: float = 6,
) -> int:
    """投稿から within_days 日以内の投稿の 👍・👎 の数を数え直す。確認した件数を返す。

    同じ投稿は interval_hours 時間に 1 回だけ確認する（毎回全件を読みに行かない）。
    Bot がイベントでその場で数えるので、これは Bot が止まっていた間の分を正すため。
    """
    now = datetime.now(UTC)
    by_platform = {c.platform: c for c in chats}
    rows = conn.execute(
        "SELECT item_id, platform, ref, checked_at FROM posts WHERE created_at >= ?",
        ((now - timedelta(days=within_days)).isoformat(timespec="seconds"),),
    ).fetchall()
    due = [
        r for r in rows
        if r["platform"] in by_platform and (
            not r["checked_at"] or datetime.fromisoformat(r["checked_at"]) <= now - timedelta(hours=interval_hours))
    ]
    for r in due:
        likes, dislikes = by_platform[r["platform"]].reactions(r["ref"]) or (None, None)
        with conn:
            conn.execute(
                "UPDATE posts SET likes = ?, dislikes = ?, checked_at = ? WHERE item_id = ? AND platform = ?",
                (likes, dislikes, db.now_iso(), r["item_id"], r["platform"]),
            )
            chatdb.sum_reactions(conn, r["item_id"])
    return len(due)


def _posts(conn: sqlite3.Connection, item_id: int) -> dict[str, str]:
    """記事のサービス → 投稿の ID。"""
    return dict(conn.execute("SELECT platform, ref FROM posts WHERE item_id = ?", (item_id,)).fetchall())


def _item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    r = conn.execute(
        "SELECT i.*, f.title AS feed_title, f.category FROM items i JOIN feeds f ON f.id = i.feed_id WHERE i.id = ?",
        (item_id,),
    ).fetchone()
    if r is None:
        raise ValueError(f"記事 {item_id} がありません")
    return r


def _pending(r: sqlite3.Row, subgenre: str | None, channels: dict[str, Channel]) -> Pending:
    return Pending(
        item_id=r["id"], feed_id=r["feed_id"], feed_title=r["feed_title"], category=r["category"], subgenre=subgenre,
        title=r["title"], url=r["url"], published=r["published"], fetched_at=r["fetched_at"], summary=r["summary"],
        md_path=r["md_path"], channels=channels, title_ja=r["title_ja"],
    )


async def retag_posts(
    conn: sqlite3.Connection,
    chats: list[Chat],
    summarizer: Summarizer | None,
    genres: dict[str, Genre] | None,
    item_ids: list[int] | None = None,
) -> tuple[int, int]:
    """投稿済みの記事のタグ（Slack はラベル）を、サブジャンルに付け直す。(書き換えた投稿数, 確認した記事数) を返す。

    サブジャンルが未判定の記事は（summarizer があれば）LLM で判定してから付ける。item_ids があればその記事だけ。
    """
    rows = conn.execute(
        "SELECT DISTINCT i.id FROM items i JOIN posts p ON p.item_id = i.id WHERE i.status = 'notified'"
    ).fetchall()
    ids = [r[0] for r in rows if item_ids is None or r[0] in set(item_ids)]
    by_genre = genre_channels(conn, [c.platform for c in chats])
    changed = 0
    for item_id in ids:
        r = _item(conn, item_id)
        subgenre = r["subgenre"]
        if subgenre is None and summarizer and r["md_path"] and Path(r["md_path"]).exists():
            body = archive.read_body(r["md_path"])
            category = await classify_item(
                conn, summarizer, genres, r["id"], r["title"], r["category"], body, allow_move=False
            )
            subgenre = category if category != r["category"] else None
        chs = by_genre.get(top_genre(subgenre or r["category"]) or UNCATEGORIZED, {})
        a = _pending(r, subgenre, chs).article()
        posts = _posts(conn, item_id)
        for c in chats:
            if c.platform in posts and c.platform in chs:
                try:
                    c.retag(chs[c.platform], posts[c.platform], a)
                except PostGoneError:
                    continue
                changed += 1
    return changed, len(ids)


def move_post(conn: sqlite3.Connection, chats: list[Chat], item_id: int, path: str) -> str:
    """記事を別のジャンル・サブジャンルに移し、結果の説明を返す。

    同じジャンルの中ならタグ（Slack はラベル）を付け替える。別のジャンルへは、新しい送り先に投稿してから元の投稿を
    消す（投稿を失わない順番。URL は変わり、👍 は引き継げない）。サブジャンルなしにする（genres suggest が新しい
    サブジャンルを作るかを判定する）。
    """
    r = _item(conn, item_id)
    top = top_genre(path) or UNCATEGORIZED
    same_genre = top == (top_genre(r["subgenre"] or r["category"]) or UNCATEGORIZED)
    if not same_genre:
        path = top
    posts = _posts(conn, item_id)
    if r["status"] != "notified" or not posts:
        with conn:
            db.update_item(conn, item_id, subgenre=path)
        return f"{r['title']}: {path} にしました（投稿前の記事）"
    chs = genre_channels(conn, [c.platform for c in chats]).get(top, {})
    if not chs:
        raise ValueError(f"{top}: 送り先が未登録です（kawarabanya discord setup --apply・slack setup --apply）")
    p = _pending(r, path, chs)
    a = p.article()
    notes = []
    for c in chats:
        if c.platform not in posts or c.platform not in chs:
            continue
        if same_genre:
            c.retag(chs[c.platform], posts[c.platform], a)
            continue
        result = c.post_article(chs[c.platform], a)
        chatdb.record_post(conn, item_id, c.platform, result)
        if result.url and p.md_path and Path(p.md_path).exists():
            archive.set_field(p.md_path, c.platform, result.url)
        try:
            c.delete_post(posts[c.platform])
        except ChatError as e:
            notes.append(f"{c.platform} の元の投稿を消せませんでした（{e}）")
    with conn:
        db.update_item(conn, item_id, subgenre=path)
    done = f"タグを {path} に付け替えました" if same_genre else f"{path} に移しました"
    return f"{r['title']}: {done}" + (f"。{'、'.join(notes)}" if notes else "")


# 全文訳の上限（字）。CPU で 1 万字ほど訳すと 2〜3 分かかる
MAX_TRANSLATE_CHARS = 12000
# 訳を区切る大きさ（字）。1 回の出力が長すぎると途中で切れる
TRANSLATE_CHUNK = 1500
# 返信 1 通の大きさ（字）。Discord の上限 2,000 字に余裕を持たせる
REPLY_CHARS = 1900


async def translate_post(conn: sqlite3.Connection, chats: list[Chat], summarizer: Summarizer, item_id: int) -> str:
    """記事の全文を日本語に訳し、<Markdown>.ja.md に保存して、投稿のスレッドに返信する。結果の説明を返す。"""
    r = conn.execute("SELECT title, title_ja, md_path FROM items WHERE id = ?", (item_id,)).fetchone()
    if r is None or not r["md_path"] or not Path(r["md_path"]).exists():
        raise ValueError(f"記事 {item_id} の本文がありません")
    body = archive.read_body(r["md_path"])
    if not is_foreign(r["title"], body):
        raise ValueError(f"「{r['title']}」は日本語の記事です")
    text = plain(body)
    cut = len(text) > MAX_TRANSLATE_CHARS
    parts = []
    for chunk in split_paragraphs(text[:MAX_TRANSLATE_CHARS], TRANSLATE_CHUNK):
        out = await summarizer.translate(chunk)
        if out is None:
            raise ValueError("翻訳に失敗しました（要約サーバーを確かめてください）")
        parts.append(out)
    translated = "\n\n".join(parts) + ("\n\n（長いので、ここまでを訳しました）" if cut else "")
    saved = Path(r["md_path"]).with_suffix(".ja.md")
    saved.write_text(f"# {r['title_ja'] or r['title']}\n\n{translated}\n", encoding="utf-8")
    posts = _posts(conn, item_id)
    sent = 0
    for c in chats:
        if c.platform in posts:
            for msg in split_paragraphs(translated, REPLY_CHARS):
                sent += c.reply(posts[c.platform], msg)
    return f"{r['title']}: {len(translated)} 字に訳しました（{saved}" + (f"、スレッドに {sent} 通" if sent else "") + "）"


def delete_duplicate_posts(conn: sqlite3.Connection, chats: list[Chat], *, apply: bool = False) -> list[sqlite3.Row]:
    """重複（duplicate）と判定した記事の投稿を返し、apply=True なら消す。"""
    by_platform = {c.platform: c for c in chats}
    rows = conn.execute(
        "SELECT i.id, i.title, p.platform, p.ref FROM items i JOIN posts p ON p.item_id = i.id"
        " WHERE i.status = 'duplicate'"
    ).fetchall()
    rows = [r for r in rows if r["platform"] in by_platform]
    if not apply:
        return rows
    for r in rows:
        by_platform[r["platform"]].delete_post(r["ref"])
        with conn:
            conn.execute("DELETE FROM posts WHERE item_id = ? AND platform = ?", (r["id"], r["platform"]))
    return rows
