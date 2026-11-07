"""パイプラインを流れる記事データ。"""

from __future__ import annotations

import hashlib
from calendar import timegm
from dataclasses import dataclass
from datetime import UTC, datetime

from . import db


@dataclass
class Item:
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
    id: int | None = None


def _entry_key(entry: dict) -> str:
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
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not stamp:
        return None
    try:
        return datetime.fromtimestamp(timegm(stamp), UTC)
    except (OverflowError, OSError, ValueError):
        return None


def from_entry(feed: db.Feed, entry: dict) -> Item:
    return Item(
        feed=feed,
        key=_entry_key(entry),
        url=(entry.get("link") or "").strip() or None,
        title=(entry.get("title") or "").strip() or None,
        author=entry.get("author") or None,
        published=_entry_datetime(entry),
        feed_html=_entry_html(entry),
    )
