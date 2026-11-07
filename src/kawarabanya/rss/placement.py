"""フィードのジャンルを LLM に決めさせる（feeds add / import-opml の --genre auto）。

フィードを取得して最新の記事の見出しを集め、フィード名と一緒に渡して、トップレベルのジャンルから 1 つを選ばせる
（記事ごとのサブジャンルは、取り込みのときに LLM が振り分ける）。
"""

from __future__ import annotations

import feedparser
import httpx

from kawarabanya.core.config import GeneralConfig
from kawarabanya.core.llm import Summarizer
from kawarabanya.rss.genres import Genre

AUTO = "auto"
# 選んだジャンルの確率がこれ未満なら決めない（genres.toml に書かず、登録だけする）
MIN_P = 0.5
MAX_TITLES = 8


async def recent_titles(url: str, general: GeneralConfig, transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """フィードの最新の記事の見出し（取れなければ空）。"""
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": general.user_agent}, timeout=general.timeout, follow_redirects=True,
            transport=transport,
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError:
        return []
    return [e.get("title", "") for e in feedparser.parse(resp.content).entries[:MAX_TITLES] if e.get("title")]


async def guess_genre(
    summarizer: Summarizer, genres: dict[str, Genre], title: str | None, titles: list[str]
) -> str | None:
    """フィード名と最近の見出しから、トップレベルのジャンルを選ぶ。決められなければ None。"""
    tops = [g for g in genres.values() if not g.is_sub]
    if not tops or not (title or titles):
        return None
    body = "\n".join(f"- {t}" for t in titles)
    # ジャンルの説明だけでは IT 企業の記事がビジネスに寄ったので、サブジャンルの名前も手がかりに添える
    options = []
    for g in tops:
        subs = "・".join(x.label for x in genres.values() if x.is_sub and x.top == g.path)
        options.append((g.label, "。".join(p for p in (g.description, subs and f"例: {subs}") if p)))
    result = await summarizer.classify(f"サイト「{title or '?'}」の最近の記事", body, options)
    if result is None or result[0] is None or result[1] < MIN_P:
        return None
    return tops[result[0]].path
