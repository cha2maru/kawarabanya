"""新着記事の取り込み（取得 → 既読判定 → 全文取得 → Markdown 保存）。"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import db, markdown
from .check import CheckResult, apply_results, check_feed
from .config import Config
from .fetcher import Fetcher
from .fulltext import fill_content
from .item import Item, from_entry

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
    conn: sqlite3.Connection, fetcher: Fetcher, cfg: Config, report: FeedReport, item: Item
) -> None:
    try:
        await fill_content(fetcher, item, cfg.fulltext)
        fetched = datetime.now(UTC)
        path = markdown.save(Path(cfg.general.archive_dir), item, fetched)
    except Exception as e:  # 1 記事の失敗で全体を止めない
        log.exception("記事の処理に失敗: %s", item.url)
        with conn:
            db.update_item(conn, item.id, status="error", error=f"{type(e).__name__}: {e}")
        report.errors.append((item, str(e)))
        return
    with conn:
        db.update_item(conn, item.id, status="saved", md_path=str(path), error=None)
    report.saved.append(item)


async def run_feeds(
    conn: sqlite3.Connection,
    feeds: list[db.Feed],
    cfg: Config,
    *,
    backfill: int = 0,
    transport: httpx.AsyncBaseTransport | None = None,
    on_report: Callable[[FeedReport], None] | None = None,
) -> list[FeedReport]:
    async with Fetcher(cfg.general, transport) as fetcher:

        async def one(feed: db.Feed) -> FeedReport:
            # 初回は全記事を見るため、条件付き GET は 2 回目以降だけ
            result = await check_feed(fetcher, feed, conditional=feed.initialized_at is not None)
            report = FeedReport(feed=feed, check=result)
            if result.status.is_ok and not result.not_modified:
                items = [from_entry(feed, e) for e in result.parsed_entries]
                selected = _select_new(conn, report, items, backfill)
                await asyncio.gather(*(_process(conn, fetcher, cfg, report, it) for it in selected))
            # 記事を取り込んでから ETag を保存する（途中で落ちても次回また取得できるように）
            apply_results(conn, [result], max_failures=cfg.general.max_failures, store_cache=True)
            if on_report:
                on_report(report)
            return report

        return await asyncio.gather(*(one(f) for f in feeds))
