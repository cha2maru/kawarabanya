"""ジャンル定義（genres.toml）の読み込みと、フィードへの割り当て。

    [news]                      # ジャンル（Discord ではフォーラム 1 つ）
    label = "ニュース"
    description = "国内・国際・社会の総合ニュース"
    feeds = ["https://...", ...]

    priority = "A: ... B: ... C: ..."   # 重要度の判定基準（省略可）
    interest = "low"            # 好き嫌い: like（まとめの上位に寄せる）/ low（まとめに載せない）/ mute（投稿もしない）

    feeds に { url = ..., item = ..., ... } と書くと HTML 取り込み（rss/htmlsource.py）、
    { url = ..., extension = "名前", ... } と書くとサイト別の拡張（rss/extensions.py）

    [news.world]                # サブジャンル（Discord ではフォーラムのタグ）
    label = "国際"
    feeds = ["https://...", ...]

フィードの移動は、URL を別のジャンルの feeds に移して `kawarabanya genres apply`。
"""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from . import extensions

RESERVED = {"label", "description", "feeds", "priority", "interest", "digest"}
# 好き嫌い（interest）。書かなければ normal（サブジャンルはジャンルの値を受け継ぐ）
INTEREST_LEVELS = {
    "like": "好き（まとめの上位に寄せる）",
    "normal": "普通",
    "low": "うすい（投稿はするが、まとめに載せない）",
    "mute": "ミュート（投稿もしない）",
}


@dataclass
class Genre:
    """genres.toml の 1 ジャンル（またはサブジャンル）。"""
    path: str  # "news" または "news/world"
    label: str
    description: str = ""
    feeds: list[str] = field(default_factory=list)
    # HTML 取り込み・拡張の設定（URL → genres.toml に書いたテーブル）
    sources: dict[str, dict] = field(default_factory=dict)
    # 重要度の判定基準（A/B/C の説明）。空ならトップレベルのジャンル → 既定の基準
    priority: str = ""
    # 好き嫌い（INTEREST_LEVELS のキー）。None なら書いていない
    interest: str | None = None

    @property
    def top(self) -> str:
        """トップレベルのジャンル（"news/world" なら "news"）。"""
        return self.path.split("/")[0]

    @property
    def is_sub(self) -> bool:
        """サブジャンルか。"""
        return "/" in self.path


def _interest(table: dict, gpath: str, path: str | Path) -> str | None:
    # digest = false は interest = "low" の古い書き方
    level = table.get("interest") or ("low" if table.get("digest") is False else None)
    if level is not None and level not in INTEREST_LEVELS:
        raise ValueError(f"{path}: [{gpath}] の interest は {' / '.join(INTEREST_LEVELS)} のどれかです: {level}")
    return level


def interest_levels(genres: dict[str, Genre] | None) -> dict[str, str]:
    """パス → 好き嫌い（書いてあるものだけ）。"""
    return {p: g.interest for p, g in (genres or {}).items() if g.interest}


def interest_of(levels: dict[str, str], category: str | None) -> str:
    """記事のカテゴリ（"news/world"）の好き嫌い。サブジャンル → ジャンルの順に探し、なければ normal。"""
    if not category:
        return "normal"
    return levels.get(category) or levels.get(top_genre(category) or "") or "normal"


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
        sources: dict[str, dict] = {}
        for entry in table.get("feeds", []):
            # 文字列はフィードの URL、テーブルは HTML 取り込みか拡張（{ url = ..., item = ..., ... }）
            if isinstance(entry, dict):
                url = entry.get("url")
                if not url:
                    raise ValueError(f"{path}: [{gpath}] の HTML 取り込み・拡張に url がありません")
                try:
                    extensions.split_entry(entry)
                except ValueError as e:
                    raise ValueError(f"{path}: {url}: {e}") from None
                sources[url] = entry
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
            sources=sources,
            priority=table.get("priority", ""),
            interest=_interest(table, gpath, path),
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
                if url in g.sources:
                    feed = _sync_source(conn, feed, url, g.sources[url], result, dry_run)
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


def _sync_source(
    conn: sqlite3.Connection, feed: db.Feed | None, url: str, entry: dict, result: ApplyResult, dry_run: bool
) -> db.Feed | None:
    """HTML 取り込み・拡張のフィードを登録し、source_type を拡張の名前に、設定を options[拡張の名前] に保存する。"""
    name, options = extensions.split_entry(entry)
    if dry_run:
        if feed is None:
            result.added.append(url)
        return feed
    if feed is None:
        db.insert_feed(conn, url, entry.get("name"), url, None)
        feed = db.find_feed_by_url(conn, url)
        result.added.append(url)
    if feed.source_type != name or feed.option(name) != options:
        conn.execute("UPDATE feeds SET source_type = ? WHERE id = ?", (name, feed.id))
        db.set_option(conn, feed.id, name, options)
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


# ===== genres.toml の書き換え（Bot・genres suggest から。手書きのコメントは残す） =====


def section_re(gpath: str) -> re.Pattern:
    """genres.toml の [news] / [news.world] の節（見出しから次の見出しの前まで）。"""
    header = re.escape(gpath.replace("/", "."))
    return re.compile(rf"^\[{header}\][^\n]*\n(?:(?!\[).*(?:\n|$))*", re.MULTILINE)


def _edit_section(file: str | Path, gpath: str, edit) -> None:
    path = Path(file)
    text = path.read_text(encoding="utf-8")
    m = section_re(gpath).search(text)
    if m is None:
        raise ValueError(f"{path} に [{gpath.replace('/', '.')}] がありません")
    path.write_text(text[: m.start()] + edit(m.group(0)) + text[m.end():], encoding="utf-8")


def set_key(section: str, name: str, value: str | None) -> str:
    """節の name = ... を value（JSON 文字列として書く）にする。None なら消す。"""
    pattern = re.compile(rf"^{name}\s*=.*\n?", re.MULTILINE)
    if value is None:
        return pattern.sub("", section, count=1)
    line = f"{name} = {json.dumps(value, ensure_ascii=False)}\n"
    if pattern.search(section):
        return pattern.sub(lambda _: line, section, count=1)
    head, _, rest = section.partition("\n")
    return f"{head}\n{line}{rest}"


def set_interest(file: str | Path, gpath: str, level: str) -> None:
    """ジャンル・サブジャンルの好き嫌いを書き換える（normal なら消す）。"""
    if level not in INTEREST_LEVELS:
        raise ValueError(f"好き嫌いは {' / '.join(INTEREST_LEVELS)} のどれかです: {level}")
    _edit_section(file, gpath, lambda sec: set_key(set_key(sec, "digest", None), "interest",
                                                   None if level == "normal" else level))


def add_feed_url(file: str | Path, gpath: str, url: str, comment: str | None = None) -> None:
    """ジャンルの feeds の末尾に URL を書き足す（feeds がなければ作る）。"""
    line = f"  {json.dumps(url)},{f'  # {comment}' if comment else ''}\n"

    def edit(sec: str) -> str:
        if m := re.search(r"^feeds\s*=\s*\[[^\]]*?(?=^\])", sec, re.MULTILINE | re.DOTALL):
            return sec[: m.end()] + line + sec[m.end():]
        head, _, rest = sec.partition("\n")
        return f"{head}\nfeeds = [\n{line}]\n{rest}"

    _edit_section(file, gpath, edit)


def remove_feed_urls(file: str | Path, urls: list[str]) -> int:
    """feeds から URL の行（文字列でも HTML 取り込みのテーブルでも）を消し、消した行数を返す。"""
    path = Path(file)
    text = path.read_text(encoding="utf-8")
    removed = 0
    for url in dict.fromkeys(urls):
        q = re.escape(json.dumps(url))
        text, n = re.subn(rf"^[ \t]*(?:{q}|\{{[^\n]*url\s*=\s*{q}[^\n]*)[^\n]*\n", "", text, flags=re.MULTILINE)
        removed += n
    path.write_text(text, encoding="utf-8")
    return removed
