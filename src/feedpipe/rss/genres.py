"""ジャンル定義（genres.toml）の読み込みと、フィードへの割り当て。

    [news]                      # ジャンル（Discord ではフォーラム 1 つ）
    label = "ニュース"
    description = "国内・国際・社会の総合ニュース"
    feeds = ["https://...", ...]

    priority = "A: ... B: ... C: ..."   # 重要度の判定基準（省略可）

    feeds に { url = ..., item = ..., ... } と書くと HTML 取り込み（rss/htmlsource.py）

    [news.world]                # サブジャンル（Discord ではフォーラムのタグ）
    label = "国際"
    feeds = ["https://...", ...]

フィードの移動は、URL を別のジャンルの feeds に移して `feedpipe genres apply`。
"""

from __future__ import annotations

import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from .htmlsource import HtmlSource

RESERVED = {"label", "description", "feeds", "priority"}


@dataclass
class Genre:
    """genres.toml の 1 ジャンル（またはサブジャンル）。"""
    path: str  # "news" または "news/world"
    label: str
    description: str = ""
    feeds: list[str] = field(default_factory=list)
    # HTML 取り込みの設定（URL → genres.toml に書いたテーブル）
    html: dict[str, dict] = field(default_factory=dict)
    # 重要度の判定基準（A/B/C の説明）。空ならトップレベルのジャンル → 既定の基準
    priority: str = ""

    @property
    def top(self) -> str:
        """トップレベルのジャンル（"news/world" なら "news"）。"""
        return self.path.split("/")[0]

    @property
    def is_sub(self) -> bool:
        """サブジャンルか。"""
        return "/" in self.path


def top_genre(category: str | None) -> str | None:
    """カテゴリのトップレベルのジャンル（"news/world" なら "news"）。なければ None。"""
    return category.split("/")[0] if category else None


def load_genres(path: str | Path) -> dict[str, Genre]:
    """genres.toml を読み、パス（"news", "news/world"）→ Genre を返す。"""
    with Path(path).open("rb") as f:
        raw = tomllib.load(f)

    genres: dict[str, Genre] = {}
    owner: dict[str, str] = {}

    def add(key: str, table: dict, parent: str | None) -> None:
        if not isinstance(table, dict):
            raise ValueError(f"{path}: [{key}] はテーブルである必要があります（不明なキー?）")  # noqa: TRY004
        gpath = f"{parent}/{key}" if parent else key
        if "/" in key:
            raise ValueError(f"{path}: ジャンル名に / は使えません: {key}")
        feeds: list[str] = []
        html: dict[str, dict] = {}
        for entry in table.get("feeds", []):
            # 文字列はフィードの URL、テーブルは HTML 取り込み（{ url = ..., item = ..., ... }）
            if isinstance(entry, dict):
                url = entry.get("url")
                if not url:
                    raise ValueError(f"{path}: [{gpath}] の HTML 取り込みに url がありません")
                try:
                    HtmlSource.from_dict({k: v for k, v in entry.items() if k not in ("url", "name")})
                except ValueError as e:
                    raise ValueError(f"{path}: {url}: {e}") from None
                html[url] = entry
            else:
                url = entry
            feeds.append(url)
            if url in owner:
                raise ValueError(f"{path}: {url} が [{owner[url]}] と [{gpath}] の両方にあります")
            owner[url] = gpath
        genres[gpath] = Genre(
            path=gpath,
            label=table.get("label", key),
            description=table.get("description", ""),
            feeds=feeds,
            html=html,
            priority=table.get("priority", ""),
        )
        for sub_key, sub in table.items():
            if sub_key in RESERVED:
                continue
            if parent:
                raise ValueError(f"{path}: サブジャンルの下にはジャンルを作れません: [{gpath}.{sub_key}]")
            add(sub_key, sub, gpath)

    for key, table in raw.items():
        add(key, table, None)
    return genres


@dataclass
class ApplyResult:
    """apply_genres の結果。"""
    added: list[str] = field(default_factory=list)  # 新しく登録した HTML 取り込みの URL
    moved: list[tuple[db.Feed, str]] = field(default_factory=list)  # (フィード, 新しいジャンル)
    unassigned: list[db.Feed] = field(default_factory=list)  # どのジャンルにも書かれていない有効なフィード
    unknown_urls: list[str] = field(default_factory=list)  # genres.toml にあるが未登録の URL


def apply_genres(
    conn: sqlite3.Connection,
    genres: dict[str, Genre],
    *,
    dry_run: bool = False,
    disable_unassigned: bool = False,
) -> ApplyResult:
    """genres.toml の割り当てをフィードの category に反映する。

    URL は現在の URL・取り込み時の URL のどちらで書いてもよい。
    書かれていない有効なフィードは category を空にし（古い名前のフォーラムに投稿されないように）、
    disable_unassigned=True なら無効にする。
    """
    result = ApplyResult()
    assigned: set[int] = set()
    with conn:
        for g in genres.values():
            for url in g.feeds:
                feed = db.find_feed_by_url(conn, url)
                if url in g.html:
                    feed = _sync_html_source(conn, feed, url, g.html[url], result, dry_run)
                if feed is None:
                    result.unknown_urls.append(url)
                    continue
                assigned.add(feed.id)
                if feed.category != g.path:
                    result.moved.append((feed, g.path))
                    if not dry_run:
                        conn.execute("UPDATE feeds SET category = ? WHERE id = ?", (g.path, feed.id))
    result.unassigned = [f for f in db.list_feeds(conn, enabled=True) if f.id not in assigned]
    if not dry_run:
        with conn:
            for f in result.unassigned:
                conn.execute("UPDATE feeds SET category = NULL WHERE id = ?", (f.id,))
                if disable_unassigned:
                    db.set_enabled(conn, f.id, False, "no_genre")
    return result


def _sync_html_source(
    conn: sqlite3.Connection, feed: db.Feed | None, url: str, entry: dict, result: ApplyResult, dry_run: bool
) -> db.Feed | None:
    """HTML 取り込みのフィードを登録し、セレクタの設定を options["html"] に保存する。"""
    selectors = {k: v for k, v in entry.items() if k not in ("url", "name")}
    if dry_run:
        if feed is None:
            result.added.append(url)
        return feed
    if feed is None:
        db.insert_feed(conn, url, entry.get("name"), url, None)
        feed = db.find_feed_by_url(conn, url)
        result.added.append(url)
    if feed.source_type != "html" or feed.option("html") != selectors:
        conn.execute("UPDATE feeds SET source_type = 'html' WHERE id = ?", (feed.id,))
        db.set_option(conn, feed.id, "html", selectors)
        feed = db.get_feed(conn, feed.id)
    return feed


def priority_criteria(genres: dict[str, Genre] | None, category: str | None) -> str | None:
    """カテゴリの判定基準。サブジャンル → トップレベルのジャンルの順に探す。"""
    if not genres or not category:
        return None
    for path in (category, top_genre(category)):
        if (g := genres.get(path)) and g.priority:
            return g.priority
    return None
