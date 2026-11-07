"""新着記事の取り込み（取得 → 既読判定 → 全文取得 → Markdown 保存）。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from kawarabanya.core import db
from kawarabanya.core.config import Config
from kawarabanya.core.llm import CHOICE_LETTERS, Summarizer
from kawarabanya.rss import archive
from kawarabanya.rss.fulltext import fill_content
from kawarabanya.rss.genres import Genre, keyword_tops, load_genres, priority_criteria
from kawarabanya.rss.item import Item, from_entry, is_recent
from kawarabanya.rss.sources.check import CheckResult, apply_results, check_feed
from kawarabanya.rss.sources.fetcher import Fetcher

log = logging.getLogger(__name__)


@dataclass
class FeedReport:
    """フィード 1 件分の取り込み結果。"""

    feed: db.Feed
    check: CheckResult
    initialized: bool = False  # 今回が初回（既存記事を既読化した）
    new: int = 0  # 未読だった記事の数
    saved: list[Item] = field(default_factory=list)
    duplicates: int = 0  # 同じ URL・同じ本文の記事が保存済みだったため飛ばした数
    errors: list[tuple[Item, str]] = field(default_factory=list)


def _select_new(
    conn: sqlite3.Connection, report: FeedReport, items: list[Item], backfill: int
) -> list[Item]:
    """未読の記事を DB に登録し、処理する記事を返す。

    初回のフィードは既存記事を既読にするだけで、新しい順に backfill 件だけ処理する。
    """
    feed = report.feed
    # 同じ記事がフィード内に重複して載っていることがあるので先に除く
    items = list({it.key: it for it in reversed(items)}.values())[::-1]
    if feed.initialized_at is None:
        report.initialized = True
        items = sorted(items, key=lambda i: i.published or datetime.min.replace(tzinfo=UTC), reverse=True)
        process, skip = items[:backfill], items[backfill:]
    else:
        process, skip = items, []

    with conn:
        selected = []
        for it in process:
            item_id = db.insert_item(conn, feed.id, it.key, it.url, it.title, _iso(it.published), "new")
            if item_id is not None:
                it.id = item_id
                selected.append(it)
        for it in skip:
            db.insert_item(conn, feed.id, it.key, it.url, it.title, _iso(it.published), "seen")
        if report.initialized:
            db.mark_initialized(conn, feed.id)
    report.new = len(selected)
    return selected


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


async def _process(
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    summarizer: Summarizer | None,
    cfg: Config,
    genres: dict[str, Genre] | None,
    report: FeedReport,
    item: Item,
) -> None:
    """記事 1 件を処理する: 本文の取得 → Markdown 保存 → （最近の記事なら）重要度の判定と要約。

    1 記事の失敗で全体を止めないよう、保存までの例外は error として記録する。
    """
    try:
        title = item.title
        await fill_content(fetcher, item, cfg.fulltext)
        if item.title != title:
            with conn:
                db.update_item(conn, item.id, title=item.title)
        fetched = datetime.now(UTC)
        content_hash = _content_hash(item.content_md)
        if dup := _find_duplicate(conn, item, content_hash):
            with conn:
                db.update_item(conn, item.id, status="duplicate", content_hash=content_hash, error=f"item {dup}")
            report.duplicates += 1
            return
        path = archive.save(Path(cfg.general.archive_dir), item, fetched)
    except Exception as e:  # 1 記事の失敗で全体を止めない
        log.exception("記事の処理に失敗: %s", item.url)
        with conn:
            db.update_item(conn, item.id, status="error", error=f"{type(e).__name__}: {e}")
        report.errors.append((item, str(e)))
        return
    with conn:
        db.update_item(conn, item.id, status="saved", md_path=str(path), content_hash=content_hash, error=None)
    report.saved.append(item)

    if summarizer and is_recent(item.published, fetched, cfg.summarize.max_age_days):
        if cfg.summarize.same_story_threshold > 0 and await _same_story(conn, summarizer, cfg, item):
            report.duplicates += 1
            return
        await _summarize(conn, summarizer, cfg, genres, item.id, item.title, item.feed.category, item.content_md, path)
        item.summary = conn.execute("SELECT summary FROM items WHERE id = ?", (item.id,)).fetchone()[0]


# これより短い本文（取れなかった・概要だけ）は、別の記事でも同じになりうるので比べない
MIN_DEDUP_CHARS = 200


def _content_hash(markdown: str | None) -> str | None:
    """本文の空白を除いたハッシュ。短すぎる本文は None。"""
    text = re.sub(r"\s+", "", markdown or "")
    return hashlib.sha1(text.encode()).hexdigest() if len(text) >= MIN_DEDUP_CHARS else None


def _find_duplicate(conn: sqlite3.Connection, item: Item, content_hash: str | None) -> int | None:
    """同じ URL か同じ本文の記事が保存済みなら、その id（Yahoo!ニュースは同じ記事を複数のフィードに載せる）。"""
    row = conn.execute(
        "SELECT id FROM items WHERE id != ? AND status IN ('saved', 'notified')"
        " AND ((url = ? AND url IS NOT NULL) OR content_hash = ?) LIMIT 1",
        (item.id, item.url, content_hash),
    ).fetchone()
    return row[0] if row else None


# 見出しの 2 文字ずつの集合の重なり（Jaccard 係数）がこれ以上の記事だけ、LLM で同じ出来事か判定する。
# 11/07 の 235 件で 0.15 → 候補 27 組（同じ出来事 15 組はすべて A の確率 0.94 以上、別の 12 組は 0.48 以下）
SAME_STORY_TITLE_SIM = 0.15
SAME_STORY_HOURS = 48
# 見出しの末尾の媒体名（「：朝日新聞」「（共同通信） - Yahoo!ニュース」）
TITLE_SOURCE = re.compile(r"\s*(?:[-|｜：]\s*[^-|｜：]{1,20}|[（(][^）)]{1,15}[）)])\s*$")


def _title_bigrams(title: str | None) -> set[str]:
    t = title or ""
    for _ in range(2):
        t = TITLE_SOURCE.sub("", t)
    t = re.sub(r"[\W_]+", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


async def _same_story(conn: sqlite3.Connection, summarizer: Summarizer, cfg: Config, item: Item) -> bool:
    """見出しの似た直近の記事と同じ出来事かを LLM で判定し、後の方を duplicate にする。item が後なら True。

    転載先ごとに本文が違う記事（朝日と時事、Yahoo!ニュースのトピックスと元記事など）を 1 件にする。
    残すのは先に保存した方（投稿済みは消さない）なので、同時に処理した 2 件が互いを判定しても同じ側が残る。
    """
    mine = _title_bigrams(item.title)
    if not mine:
        return False
    since = (datetime.now(UTC) - timedelta(hours=SAME_STORY_HOURS)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, title, md_path, status FROM items WHERE id != ? AND status IN ('saved', 'notified')"
        " AND fetched_at >= ?",
        (item.id, since),
    ).fetchall()
    for r in rows:
        theirs = _title_bigrams(r["title"])
        if not theirs or len(mine & theirs) / len(mine | theirs) < SAME_STORY_TITLE_SIM:
            continue
        body = archive.read_body(r["md_path"]) if r["md_path"] and Path(r["md_path"]).exists() else ""
        p = await summarizer.same_story((item.title, item.content_md), (r["title"], body))
        if p is None or p < cfg.summarize.same_story_threshold:
            continue
        with conn:
            if r["status"] == "notified" or r["id"] < item.id:
                db.update_item(conn, item.id, status="duplicate", error=f"same story: item {r['id']}")
                return True
            conn.execute(
                "UPDATE items SET status = 'duplicate', error = ? WHERE id = ? AND status = 'saved'",
                (f"same story: item {item.id}", r["id"]),
            )
    return False


async def _summarize(
    conn: sqlite3.Connection,
    summarizer: Summarizer,
    cfg: Config,
    genres: dict[str, Genre] | None,
    item_id: int,
    title: str | None,
    category: str | None,
    content_md: str | None,
    md_path: str | Path,
    priority: float | None = None,
) -> str:
    """サブジャンルと重要度を判定し、基準以上なら要約する。"summarized" / "low"（基準未満）/ "failed" を返す。"""
    category = await classify_item(conn, summarizer, genres, item_id, title, category, content_md)
    threshold = cfg.summarize.priority_threshold
    if threshold > 0:
        if priority is None:
            priority = await summarizer.priority(title, content_md, priority_criteria(genres, category))
            if priority is not None:
                with conn:
                    db.update_item(conn, item_id, priority=priority)
        # 判定に失敗したとき（None）は、取りこぼさないよう要約する
        if priority is not None and priority < threshold:
            return "low"
    summary = await summarizer.summarize(title, content_md)
    if not summary:
        return "failed"
    archive.add_summary(md_path, summary)
    with conn:
        db.update_item(conn, item_id, summary=summary)
    return "summarized"


# 全サブジャンルから選ばせた結果の確率が、フィードのジャンルの中ならこれ以上、別のジャンルならこれ以上で採用する。
# 11/07 の 220 件で、別のジャンルへの 0.8 以上はほぼ正しく（ゴジラの映画 tech → culture/movie 0.93 など）、0.6 未満は誤りが多い
MATCH_P = 0.5
MOVE_P = 0.8


async def classify_item(
    conn: sqlite3.Connection,
    summarizer: Summarizer,
    genres: dict[str, Genre] | None,
    item_id: int,
    title: str | None,
    category: str | None,
    content_md: str | None,
    *,
    allow_move: bool = True,
) -> str | None:
    """トップレベルのジャンルのフィードの記事を、LLM で全サブジャンルから 1 つに振り分けて items.subgenre に記録する。

    フィードのジャンル（RSS の URL の分類）とは別のジャンルのサブジャンルも選べる（MOVE_P 以上のとき）。
    どれにも当てはまらなければ items.subgenre はトップレベルのジャンル（"news"）にする（新しいサブジャンルの候補）。
    タイトルに genres.toml の keywords の語があれば、先にそのジャンルのサブジャンルだけから選ばせる。
    外れたら全サブジャンルから選び直す（「ゴジラのフィギュア」のように語のジャンルと違う記事があるため）。
    サブジャンルに固定したフィード（genres.toml でサブジャンルの下に書いたもの）は判定しない。
    allow_move=False なら、フィードのジャンルの中だけで選ぶ（投稿済みの記事はフォーラムを移せないので）。
    振り分けたカテゴリ（判定できなければ元のカテゴリ）を返す。
    """
    if not genres or not category or "/" in category:
        return category
    subs = [g for g in genres.values() if g.is_sub and (allow_move or g.top == category)]
    hits = keyword_tops(genres, title) if allow_move else keyword_tops(genres, title) & {category}

    async def choose(candidates: list[Genre]) -> tuple[int | None, float] | None:
        options = [(f"{genres[g.top].label if g.top in genres else g.top} / {g.label}", g.description) for g in candidates]
        return await summarizer.classify(title, content_md, options)

    if narrowed := [g for g in subs if g.top in hits]:
        result = await choose(narrowed)
        if result and result[0] is not None and result[1] >= MATCH_P:
            path = narrowed[result[0]].path
            with conn:
                db.update_item(conn, item_id, subgenre=path)
            return path
    if len(subs) > len(CHOICE_LETTERS):
        # 1 回で選べる数（51）を超えたら、先にジャンルを選ばせ、その中のサブジャンルから選ばせる
        tops = [g for g in genres.values() if not g.is_sub and any(s.top == g.path for s in subs)]
        first = await summarizer.classify(title, content_md, [(g.label, g.description) for g in tops])
        top = tops[first[0]].path if first and first[0] is not None and first[1] >= MOVE_P else category
        subs = [g for g in subs if g.top == top]
    if not subs:
        return category
    result = await choose(subs)
    if result is None:
        return category
    idx, p = result
    sub = subs[idx] if idx is not None else None
    if sub and p >= (MATCH_P if sub.top == category or sub.top in hits else MOVE_P):
        path = sub.path
    else:
        path = hits.copy().pop() if len(hits) == 1 else category  # どれにも当てはまらない
    with conn:
        db.update_item(conn, item_id, subgenre=path)
    return path


def load_genres_if_any(cfg: Config) -> dict[str, Genre] | None:
    """genres.toml があれば読む。"""
    path = Path(cfg.general.genres_file)
    return load_genres(path) if path.exists() else None


async def summarize_pending(
    conn: sqlite3.Connection, summarizer: Summarizer, cfg: Config, limit: int | None = None
) -> Counter:
    """保存済み・未通知で要約がない最近の記事を、重要度の判定を通して要約する。結果ごとの件数を返す。"""
    genres = load_genres_if_any(cfg)
    threshold = cfg.summarize.priority_threshold
    rows = conn.execute(
        "SELECT i.id, i.title, i.published, i.fetched_at, i.md_path, i.priority, f.category"
        " FROM items i JOIN feeds f ON f.id = i.feed_id"
        " WHERE i.status = 'saved' AND i.summary IS NULL AND i.md_path IS NOT NULL ORDER BY i.published DESC"
    ).fetchall()
    targets = [
        r for r in rows
        if is_recent(r["published"], r["fetched_at"], cfg.summarize.max_age_days)
        # 判定済みで基準未満のものは、もう判定も要約もしない
        and not (threshold > 0 and r["priority"] is not None and r["priority"] < threshold)
    ][:limit]
    results: Counter = Counter()
    for r in targets:
        if not Path(r["md_path"]).exists():
            continue
        body = archive.read_body(r["md_path"])
        results[await _summarize(
            conn, summarizer, cfg, genres, r["id"], r["title"], r["category"], body, r["md_path"], r["priority"]
        )] += 1
        if not summarizer.available:
            break
    return results


async def run_feeds(
    conn: sqlite3.Connection,
    feeds: list[db.Feed],
    cfg: Config,
    *,
    backfill: int = 0,
    summarizer: Summarizer | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    on_report: Callable[[FeedReport], None] | None = None,
) -> list[FeedReport]:
    """フィードを取得し、未読の記事を処理する（run の本体）。

    初回のフィードは既存記事を既読にするだけ（backfill 件だけ処理）。記事を処理し終えてから ETag を保存するので、途中で落ちても次回また取得できる。
    """
    genres = load_genres_if_any(cfg)
    async with Fetcher(cfg.general, transport) as fetcher:

        async def one(feed: db.Feed) -> FeedReport:
            # 初回は全記事を見るため、条件付き GET は 2 回目以降だけ
            result = await check_feed(fetcher, feed, conditional=feed.initialized_at is not None)
            report = FeedReport(feed=feed, check=result)
            if result.status.is_ok and not result.not_modified:
                items = [from_entry(feed, e) for e in result.parsed_entries]
                selected = _select_new(conn, report, items, backfill)
                await asyncio.gather(*(_process(conn, fetcher, summarizer, cfg, genres, report, it) for it in selected))
            # 記事を取り込んでから ETag を保存する（途中で落ちても次回また取得できるように）
            apply_results(conn, [result], max_failures=cfg.general.max_failures, store_cache=True)
            if on_report:
                on_report(report)
            return report

        return await asyncio.gather(*(one(f) for f in feeds))
