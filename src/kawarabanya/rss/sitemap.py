"""組み込みの拡張 sitemap: サイトマップ（sitemap.xml）の新しい URL を記事として取り込む。

RSS も CSS セレクタで取れる一覧ページもないサイト向け。genres.toml の feeds に

    { url = "https://example.com/sitemap.xml", extension = "sitemap", link_pattern = "/news/" }

と書く。サイトマップの目次（sitemapindex）なら、更新の新しい子のサイトマップを children 個（既定 3）まで読む。
記事は lastmod の新しい順に MAX_ITEMS 件。タイトルは Google ニュース用のサイトマップの news:title、
なければ空にして、本文を取るときに記事ページの <title> を使う（fulltext.fill_content）。
"""

from __future__ import annotations

import re
from datetime import datetime

import lxml.etree

from .fetcher import Fetcher
from .htmlsource import MAX_ITEMS, normalize_url

OPTIONS = {"link_pattern", "children"}


def validate(options: dict) -> None:
    """設定（link_pattern・children）を確かめる。"""
    if unknown := set(options) - OPTIONS:
        raise ValueError(f"sitemap に不明なキーがあります: {', '.join(sorted(unknown))}")
    if "link_pattern" in options:
        re.compile(options["link_pattern"])


def _local(el) -> str:
    return lxml.etree.QName(el).localname


def _child_text(el, name: str) -> str | None:
    for c in el.iter():
        if c is not el and isinstance(c.tag, str) and _local(c) == name and c.text:
            return c.text.strip()
    return None


def _lastmod(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)  # 3.11 以降は末尾の Z も読める
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def _parse(content: bytes) -> tuple[str, list[tuple[str, datetime | None, str | None]]]:
    """サイトマップを読み、(urlset か sitemapindex, [(URL, 更新日時, news:title)]) を返す。"""
    root = lxml.etree.fromstring(content, parser=lxml.etree.XMLParser(recover=True, resolve_entities=False))
    kind = _local(root)
    rows = []
    for el in root:
        if not isinstance(el.tag, str) or _local(el) not in ("url", "sitemap"):
            continue
        loc = _child_text(el, "loc")
        if loc:
            rows.append((loc, _lastmod(_child_text(el, "lastmod")), _child_text(el, "title")))
    return kind, rows


def _newest(rows: list) -> list:
    return sorted(rows, key=lambda r: r[1].timestamp() if r[1] else 0, reverse=True)


async def fetch(fetcher: Fetcher, url: str, options: dict) -> list[dict]:
    """サイトマップ（目次なら子も）を取得し、新しい URL を feedparser の entry の形で返す。"""
    resp = await fetcher.get(url)
    resp.raise_for_status()
    kind, rows = _parse(resp.content)
    if kind == "sitemapindex":
        children, rows = _newest(rows)[: int(options.get("children", 3))], []
        for child, *_ in children:
            r = await fetcher.get(child)
            r.raise_for_status()
            rows += _parse(r.content)[1]
    pattern = re.compile(options["link_pattern"]) if options.get("link_pattern") else None
    entries: dict[str, dict] = {}
    for loc, modified, title in _newest(rows):
        link = normalize_url(loc)
        if (pattern and not pattern.search(link)) or link in entries:
            continue
        entries[link] = {
            "id": link,
            "link": link,
            "title": title or "",
            "published_parsed": modified.utctimetuple() if modified else None,
            "summary": "",
        }
        if len(entries) >= MAX_ITEMS:
            break
    return list(entries.values())
