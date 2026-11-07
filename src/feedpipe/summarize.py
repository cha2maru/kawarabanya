"""ローカル LLM（OpenAI 互換 API）による記事の要約。"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Self

import httpx

from .config import SummarizeConfig

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "あなたはニュース記事を要約するアシスタントです。"
    "記事の要点を日本語で2〜3文、合計120字程度にまとめてください。"
    "記事に書かれていないことは書かないでください。前置きは不要です。"
)

CJK = r"぀-ヿ㐀-鿿！-｠"


def prepare_input(title: str | None, markdown: str, max_chars: int) -> str:
    """画像・リンク先 URL を除いて文字数を減らし、先頭 max_chars 字に切る。"""
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()[:max_chars]
    return f"タイトル: {title or '(無題)'}\n\n本文:\n{body}"


def clean_output(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Qwen 系が入れがちな、日本語と英数字の間の空白を取る（「9 日」→「9日」）
    text = re.sub(rf"(?<=[{CJK}]) +(?=[0-9A-Za-z])", "", text)
    text = re.sub(rf"(?<=[0-9A-Za-z%]) +(?=[{CJK}])", "", text)
    return text


class Summarizer:
    """要約 API のクライアント。サーバーに繋がらなければ、以降の要約はすぐに諦める。"""

    def __init__(self, cfg: SummarizeConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.available = True
        # CPU 推論は 1 件ずつしか進まないので、待ち時間がタイムアウトに含まれないよう直列にする
        self._sem = asyncio.Semaphore(1)
        self._client = httpx.AsyncClient(
            base_url=cfg.endpoint.rstrip("/"), transport=transport, timeout=cfg.timeout
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def summarize(self, title: str | None, markdown: str | None) -> str | None:
        if not self.available or not markdown:
            return None
        body = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prepare_input(title, markdown, self.cfg.max_input_chars)},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            # Qwen3.5 などの思考モードを切る（対応しないモデルでは無視される）
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with self._sem:
            try:
                resp = await self._client.post("/chat/completions", json=body)
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
            except httpx.ConnectError as e:
                log.warning("要約サーバーに接続できないため、今回は要約しません: %s", e)
                self.available = False
                return None
            except (httpx.HTTPError, KeyError, ValueError) as e:
                log.warning("要約に失敗: %s (%s)", title, e)
                return None
        return clean_output(text) or None
