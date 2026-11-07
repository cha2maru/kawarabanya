"""サイトの URL から RSS / Atom フィードを探す（feeds add 用）。"""

from __future__ import annotations

import re
from urllib.parse import urljoin

import feedparser
import httpx

from kawarabanya.core.config import GeneralConfig
from kawarabanya.rss.sources.htmlsource import decode_html

# <link rel="alternate" type="application/rss+xml" href="..."> の href
LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
FEED_TYPE_RE = re.compile(r"""type=["']application/(?:rss|atom|rdf)\+xml["']""", re.IGNORECASE)
HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
# 案内がないときに試すよくある場所
COMMON_PATHS = ["feed", "rss", "rss.xml", "atom.xml", "index.xml", "index.rdf", "feed.xml"]


def _as_feed(resp: httpx.Response) -> str | None:
    """応答がフィードならそのタイトル（なければ空文字）、フィードでなければ None。"""
    if not resp.is_success:
        return None
    parsed = feedparser.parse(resp.content)
    if not parsed.get("version") or not parsed.entries:
        return None
    return parsed.feed.get("title", "")


async def discover(
    url: str, general: GeneralConfig, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[str, str] | None:
    """url がフィードならそれ、ページならページ内の案内かよくある場所のフィードを探し、(URL, タイトル) を返す。"""
    async with httpx.AsyncClient(
        headers={"User-Agent": general.user_agent}, timeout=general.timeout, follow_redirects=True, transport=transport
    ) as client:
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            return None
        if (title := _as_feed(resp)) is not None:
            return str(resp.url), title
        base = str(resp.url)
        html, _ = decode_html(resp.content, resp.charset_encoding)
        linked = [
            urljoin(base, h.group(1))
            for tag in LINK_RE.findall(html)
            if FEED_TYPE_RE.search(tag) and (h := HREF_RE.search(tag))
        ]
        for candidate in dict.fromkeys([*linked, *(urljoin(base.rstrip("/") + "/", p) for p in COMMON_PATHS)]):
            try:
                r = await client.get(candidate)
            except httpx.HTTPError:
                continue
            if (title := _as_feed(r)) is not None:
                return str(r.url), title
    return None
