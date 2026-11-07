"""フィードの取得と死活チェック。"""

from __future__ import annotations

import asyncio
import socket
import sqlite3
import time
from calendar import timegm
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

import feedparser
import httpx

from .. import db
from ..config import GeneralConfig
from .fetcher import Fetcher

PERMANENT_REDIRECTS = {301, 308}


class Status(StrEnum):
    OK = "ok"
    NOT_FEED = "not_feed"  # 応答はあるがフィードとして解析できない
    GONE = "gone"  # 404 / 410
    FORBIDDEN = "forbidden"  # 401 / 403（ボット対策のことも多い）
    HTTP_ERROR = "http_error"  # その他の 4xx / 5xx
    DNS = "dns"  # ホスト名が存在しない
    NETWORK = "network"  # 接続失敗・タイムアウト・TLS エラーなど

    @property
    def is_ok(self) -> bool:
        return self is Status.OK

    @property
    def is_permanent(self) -> bool:
        """回復の見込みが薄い失敗か（`check --disable-dead` で即無効化する対象）。"""
        return self in {Status.NOT_FEED, Status.GONE, Status.DNS}


@dataclass
class CheckResult:
    feed: db.Feed
    status: Status
    http_status: int | None = None
    # 恒久的なリダイレクト（301/308 のみ）の最終 URL。一時的なリダイレクトは None。
    moved_to: str | None = None
    feed_title: str | None = None
    entries: int = 0
    # フィード内で最も新しい記事の日時（日付のない記事しかなければ None）
    latest_entry: datetime | None = None
    error: str | None = None
    elapsed: float = 0.0
    # 以下は取得（run）で使う
    not_modified: bool = False  # 304 が返った
    parsed_entries: list = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None


def _is_name_not_found(exc: BaseException) -> bool:
    """例外の連鎖をたどり、ホスト名が存在しない（一時的な名前解決失敗ではない）かを判定する。"""
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, socket.gaierror):
            return e.errno in {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -5)}
        if "Name or service not known" in str(e):
            return True
        e = e.__cause__ or e.__context__
    return False


def _latest_entry(entries: list) -> datetime | None:
    stamps = [
        t
        for e in entries
        if (t := e.get("published_parsed") or e.get("updated_parsed")) is not None
    ]
    if not stamps:
        return None
    return datetime.fromtimestamp(timegm(max(stamps)), UTC)


def _classify_http(code: int) -> Status:
    if code in (404, 410):
        return Status.GONE
    if code in (401, 403):
        return Status.FORBIDDEN
    return Status.HTTP_ERROR


async def check_feed(
    fetcher: Fetcher, feed: db.Feed, *, conditional: bool = False
) -> CheckResult:
    """フィードを取得して状態を判定する。

    conditional=True なら前回の ETag / Last-Modified を送り、304 なら not_modified になる。
    """
    started = time.monotonic()
    result = CheckResult(feed=feed, status=Status.NETWORK)
    headers: dict[str, str] = {}
    if conditional:
        if feed.etag:
            headers["If-None-Match"] = feed.etag
        if feed.last_modified:
            headers["If-Modified-Since"] = feed.last_modified
    try:
        resp = await fetcher.get(feed.url, headers=headers)
    except httpx.TooManyRedirects as e:
        result.error = f"リダイレクトが多すぎます: {e}"
    except httpx.TimeoutException:
        result.error = "タイムアウト"
    except httpx.HTTPError as e:
        if _is_name_not_found(e):
            result.status = Status.DNS
            result.error = f"ホストが見つかりません: {urlsplit(feed.url).hostname}"
        else:
            result.error = f"{type(e).__name__}: {e}"
    else:
        result.http_status = resp.status_code
        if resp.status_code == 304:
            result.status = Status.OK
            result.not_modified = True
        elif resp.is_success:
            # 移転先が正常に応答した場合だけ URL を更新対象にする
            if resp.history and all(h.status_code in PERMANENT_REDIRECTS for h in resp.history):
                final = str(resp.url)
                if final != feed.url:
                    result.moved_to = final
            parsed = feedparser.parse(resp.content, response_headers=dict(resp.headers))
            if parsed.version:
                result.status = Status.OK
                result.feed_title = parsed.feed.get("title")
                result.parsed_entries = parsed.entries
                result.entries = len(parsed.entries)
                result.latest_entry = _latest_entry(parsed.entries)
                result.etag = resp.headers.get("etag")
                result.last_modified = resp.headers.get("last-modified")
            else:
                result.status = Status.NOT_FEED
                result.error = f"フィードではありません（{resp.headers.get('content-type', '?')}）"
        else:
            result.status = _classify_http(resp.status_code)
            result.error = f"HTTP {resp.status_code}"
    result.elapsed = time.monotonic() - started
    return result


async def check_feeds(
    feeds: list[db.Feed],
    cfg: GeneralConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    on_result: Callable[[CheckResult], None] | None = None,
) -> list[CheckResult]:
    async with Fetcher(cfg, transport) as fetcher:

        async def one(feed: db.Feed) -> CheckResult:
            r = await check_feed(fetcher, feed)
            if on_result:
                on_result(r)
            return r

        return await asyncio.gather(*(one(f) for f in feeds))


@dataclass
class ApplyStats:
    moved: int = 0
    disabled: int = 0
    merged: int = 0  # 移転先がすでに登録済みだったため無効化したもの


def apply_results(
    conn: sqlite3.Connection,
    results: list[CheckResult],
    *,
    max_failures: int,
    disable_dead: bool = False,
    store_cache: bool = False,
) -> ApplyStats:
    """チェック結果を DB に反映する（URL 更新・失敗回数・自動無効化）。

    store_cache=True なら ETag / Last-Modified を保存する（記事を取り込んだ run のときだけ）。
    """
    stats = ApplyStats()
    now = db.now_iso()
    with conn:
        for r in results:
            f = r.feed
            if r.moved_to:
                other = db.find_feed_by_url(conn, r.moved_to)
                if other and other.id != f.id:
                    db.set_enabled(conn, f.id, False, f"duplicate_of:{other.id}")
                    stats.merged += 1
                    continue
                conn.execute("UPDATE feeds SET url = ? WHERE id = ?", (r.moved_to, f.id))
                stats.moved += 1

            if r.status.is_ok:
                conn.execute(
                    "UPDATE feeds SET last_fetched_at = ?, last_success_at = ?, last_status = ?,"
                    " last_error = NULL, fail_count = 0,"
                    " latest_entry_at = COALESCE(?, latest_entry_at),"
                    " title = COALESCE(title, ?) WHERE id = ?",
                    (
                        now,
                        now,
                        r.status.value,
                        r.latest_entry.isoformat(timespec="seconds") if r.latest_entry else None,
                        r.feed_title,
                        f.id,
                    ),
                )
                if store_cache and not r.not_modified:
                    conn.execute(
                        "UPDATE feeds SET etag = ?, last_modified = ? WHERE id = ?",
                        (r.etag, r.last_modified, f.id),
                    )
                continue

            fail_count = f.fail_count + 1
            conn.execute(
                "UPDATE feeds SET last_fetched_at = ?, last_status = ?, last_error = ?,"
                " fail_count = ? WHERE id = ?",
                (now, r.status.value, r.error, fail_count, f.id),
            )
            if disable_dead and r.status.is_permanent:
                db.set_enabled(conn, f.id, False, r.status.value)
                stats.disabled += 1
            elif fail_count >= max_failures:
                db.set_enabled(conn, f.id, False, f"fail_x{fail_count}:{r.status.value}")
                stats.disabled += 1
    return stats
