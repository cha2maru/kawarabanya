"""パイプラインを流れる記事データ。"""

from __future__ import annotations

import hashlib
import re
from calendar import timegm
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .. import db


@dataclass
class Item:
    """パイプラインを流れる 1 記事。取得から保存・要約までの途中結果も持つ。"""
    feed: db.Feed
    key: str
    url: str | None
    title: str | None
    author: str | None
    published: datetime | None
    # フィードに含まれていた本文（HTML）
    feed_html: str | None
    content_md: str | None = None
    # content_md の出どころ: article（元記事から抽出）/ feed（フィードの本文）/ none
    content_source: str = "none"
    # 元記事のページの文字コード（euc-jp・shift_jis など。保存は常に UTF-8）
    charset: str | None = None
    summary: str | None = None
    id: int | None = None


def _entry_key(entry: dict) -> str:
    """記事を見分けるキー: guid、なければ link、それもなければタイトルと要約のハッシュ。"""
    if key := (entry.get("id") or entry.get("link")):
        return key.strip()
    raw = f"{entry.get('title', '')}\n{entry.get('summary', '')}"
    return "sha1:" + hashlib.sha1(raw.encode()).hexdigest()


def _entry_html(entry: dict) -> str | None:
    """フィード内の本文のうち、最も長いものを返す（content があれば summary より優先）。"""
    candidates = [c.get("value", "") for c in entry.get("content", [])]
    candidates.append(entry.get("summary", ""))
    best = max(candidates, key=len)
    return best or None


def _entry_datetime(entry: dict) -> datetime | None:
    """記事の公開日時（なければ更新日時）。おかしな値なら None。"""
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not stamp:
        return None
    try:
        return datetime.fromtimestamp(timegm(stamp), UTC)
    except (OverflowError, OSError, ValueError):
        return None


def from_entry(feed: db.Feed, entry: dict) -> Item:
    """feedparser の記事（entry）から Item を作る。"""
    return Item(
        feed=feed,
        key=_entry_key(entry),
        url=(entry.get("link") or "").strip() or None,
        # Google ニュースなどは見出しに <b> などのタグを含める
        title=re.sub(r"<[^>]+>", "", entry.get("title") or "").strip() or None,
        author=entry.get("author") or None,
        published=_entry_datetime(entry),
        feed_html=_entry_html(entry),
    )


def is_recent(published: datetime | str | None, fallback: datetime | str, days: int) -> bool:
    """公開日時（なければ fallback = 取得日時）が days 日以内か。"""
    when = published or fallback
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    return when >= datetime.now(UTC) - timedelta(days=days)
