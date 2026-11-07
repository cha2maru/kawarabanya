"""新着記事の取り込み（取得 → 既読判定 → 全文取得 → Markdown 保存）。"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .. import db
from ..config import Config
from ..llm import Summarizer
from . import archive
from .check import CheckResult, apply_results, check_feed
from .fetcher import Fetcher
from .fulltext import fill_content
from .genres import Genre, load_genres, priority_criteria
from .item import Item, from_entry, is_recent

log = logging.getLogger(__name__)


@dataclass
class FeedReport:
    """フィード 1 件分の取り込み結果。"""

    feed: db.Feed
    check: CheckResult
    initialized: bool = False  # 今回が初回（既存記事を既読化した）
    new: int = 0  # 未読だった記事の数
    saved: list[Item] = field(default_factory=list)
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
        await fill_content(fetcher, item, cfg.fulltext)
        fetched = datetime.now(UTC)
        path = archive.save(Path(cfg.general.archive_dir), item, fetched)
    except Exception as e:  # 1 記事の失敗で全体を止めない
        log.exception("記事の処理に失敗: %s", item.url)
        with conn:
            db.update_item(conn, item.id, status="error", error=f"{type(e).__name__}: {e}")
        report.errors.append((item, str(e)))
        return
    with conn:
        db.update_item(conn, item.id, status="saved", md_path=str(path), error=None)
    report.saved.append(item)

    if summarizer and is_recent(item.published, fetched, cfg.summarize.max_age_days):
        await _summarize(conn, summarizer, cfg, genres, item.id, item.title, item.feed.category, item.content_md, path)
        item.summary = conn.execute("SELECT summary FROM items WHERE id = ?", (item.id,)).fetchone()[0]


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
    category = await _classify(conn, summarizer, genres, item_id, title, category, content_md)
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


async def _classify(
    conn: sqlite3.Connection,
    summarizer: Summarizer,
    genres: dict[str, Genre] | None,
    item_id: int,
    title: str | None,
    category: str | None,
    content_md: str | None,
) -> str | None:
    """トップレベルのジャンルのフィードの記事を、LLM でサブジャンルに振り分けて items.subgenre に記録する。

    サブジャンルに固定したフィード（genres.toml でサブジャンルの下に書いたもの）は判定しない。
    振り分けたカテゴリ（なければ元のカテゴリ）を返す。
    """
    if not genres or not category or "/" in category:
        return category
    subs = [g for g in genres.values() if g.is_sub and g.top == category]
    if not subs:
        return category
    result = await summarizer.classify(title, content_md, [(g.label, g.description) for g in subs])
    if result is None or result[0] is None:
        return category
    sub = subs[result[0]].path
    with conn:
        db.update_item(conn, item_id, subgenre=sub)
    return sub


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
