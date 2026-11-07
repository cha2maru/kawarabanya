"""同時接続数を制限した HTTP クライアント。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Self
from urllib.parse import urlsplit

import httpx

from kawarabanya.core.config import GeneralConfig


class Fetcher:
    """全体の同時接続数と、ホストごとの同時接続数を制限して GET する。"""

    def __init__(self, cfg: GeneralConfig, transport: httpx.AsyncBaseTransport | None = None):
        self._global = asyncio.Semaphore(cfg.concurrency)
        self._hosts: defaultdict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(cfg.per_host)
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=True,
            timeout=cfg.timeout,
            headers={"User-Agent": cfg.user_agent},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """url を GET する。全体とホストごとの同時接続数の枠が空くまで待つ。"""
        host = urlsplit(url).hostname or ""
        async with self._global, self._hosts[host]:
            return await self._client.get(url, headers=headers)
