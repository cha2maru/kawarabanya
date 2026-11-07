"""元記事を取得し、本文を Markdown として抽出する。"""

from __future__ import annotations

import asyncio
import logging

import httpx
import trafilatura

from ..config import FulltextConfig
from .fetcher import Fetcher
from .item import Item

log = logging.getLogger(__name__)


def html_to_markdown(html: str | bytes, url: str | None, cfg: FulltextConfig) -> str | None:
    return trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_images=cfg.include_images,
        include_formatting=True,
        include_tables=True,
        include_comments=False,
    )


async def _article_markdown(fetcher: Fetcher, url: str, cfg: FulltextConfig) -> str | None:
    try:
        resp = await fetcher.get(url)
    except httpx.HTTPError as e:
        log.info("本文の取得に失敗: %s (%s)", url, e)
        return None
    if not resp.is_success or "html" not in resp.headers.get("content-type", "html"):
        log.info("本文の取得に失敗: %s (HTTP %s)", url, resp.status_code)
        return None
    # 文字コードの判定は trafilatura に任せるため bytes のまま渡す
    return await asyncio.to_thread(html_to_markdown, resp.content, str(resp.url), cfg)


async def fill_content(fetcher: Fetcher, item: Item, cfg: FulltextConfig) -> None:
    """item.content_md と item.content_source を埋める。"""
    feed_md = None
    if item.feed_html:
        if "<" in item.feed_html:
            feed_md = await asyncio.to_thread(html_to_markdown, item.feed_html, item.url, cfg)
        else:  # タグを含まないプレーンテキスト
            feed_md = item.feed_html.strip()

    article_md = None
    if cfg.enabled and item.url and not item.feed.option("no_fulltext"):
        article_md = await _article_markdown(fetcher, item.url, cfg)

    if article_md and (len(article_md) >= cfg.min_length or len(article_md) >= len(feed_md or "")):
        item.content_md, item.content_source = article_md, "article"
    elif feed_md:
        item.content_md, item.content_source = feed_md, "feed"
    elif article_md:
        item.content_md, item.content_source = article_md, "article"
