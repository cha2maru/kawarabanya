"""ローカル LLM（OpenAI 互換 API）による記事の要約。"""

from __future__ import annotations

import asyncio
import logging
import math
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

# 重要度の判定。ラベルの割合は偏る（A が 7 割など）ので、A の確率で並べて上位を選ぶ使い方をする
PRIORITY_PROMPT = (
    "あなたはニュースの編集者です。記事の見出しと冒頭を読み、読者にとっての重要度を判定します。"
    "{criteria}"
    "A・B・C のいずれか 1 文字だけで答えてください。"
)
DEFAULT_CRITERIA = (
    "A: 高い（多くの人に影響する出来事・大きな発表・転換点） "
    "B: 普通（一般的な新製品・アップデート・解説） "
    "C: 低い（セール・広告・小ネタ・ランキング）"
)
LEAD_CHARS = 200

CJK = r"぀-ヿ㐀-鿿！-｠"


def prepare_input(title: str | None, markdown: str, max_chars: int) -> str:
    """画像・リンク先 URL を除いて文字数を減らし、先頭 max_chars 字に切る。"""
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()[:max_chars]
    return f"タイトル: {title or '(無題)'}\n\n本文:\n{body}"


def lead(markdown: str, chars: int = LEAD_CHARS) -> str:
    """本文の冒頭を、画像・リンク先 URL・改行を除いたプレーンテキストで返す。"""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", markdown)
    return re.sub(r"\s+", " ", text).strip()[:chars]


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

    async def _chat(self, body: dict, what: str) -> dict | None:
        """API を呼び、応答の JSON を返す。失敗したら None（接続できなければ以降も諦める）。"""
        if not self.available:
            return None
        body = {
            "model": self.cfg.model,
            # Qwen3.5 などの思考モードを切る（対応しないモデルでは無視される）
            "chat_template_kwargs": {"enable_thinking": False},
            **body,
        }
        async with self._sem:
            try:
                resp = await self._client.post("/chat/completions", json=body)
                resp.raise_for_status()
                return resp.json()
            except httpx.ConnectError as e:
                log.warning("要約サーバーに接続できないため、今回は要約・判定しません: %s", e)
                self.available = False
            except (httpx.HTTPError, ValueError) as e:
                log.warning("%sに失敗: %s", what, e)
        return None

    async def summarize(self, title: str | None, markdown: str | None) -> str | None:
        if not markdown:
            return None
        data = await self._chat(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prepare_input(title, markdown, self.cfg.max_input_chars)},
                ],
                "temperature": self.cfg.temperature,
                "max_tokens": self.cfg.max_tokens,
            },
            f"要約（{title}）",
        )
        if data is None:
            return None
        try:
            return clean_output(data["choices"][0]["message"]["content"]) or None
        except (KeyError, IndexError, TypeError):
            return None

    async def priority(self, title: str | None, markdown: str | None, criteria: str | None = None) -> float | None:
        """見出しと冒頭から重要度を判定し、「A（高い）」の確率（0〜1）を返す。

        文章を書かせず 1 トークン（A/B/C）だけを出させ、その確率を読む（Jev 風の判定）。
        出力が 1 トークンなので、時間はほぼ入力の読み込みだけ（CPU で 1〜2 秒）。
        """
        data = await self._chat(
            {
                "messages": [
                    {"role": "system", "content": PRIORITY_PROMPT.format(criteria=criteria or DEFAULT_CRITERIA)},
                    {"role": "user", "content": f"見出し: {title or '(無題)'}\n冒頭: {lead(markdown or '')}"},
                ],
                "temperature": 0,
                "max_tokens": 1,
                "logprobs": True,
                "top_logprobs": 10,
                "grammar": 'root ::= "A" | "B" | "C"',  # llama-server の文法制約で A/B/C の 1 文字に限る
            },
            f"重要度の判定（{title}）",
        )
        try:
            top = data["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        except (KeyError, IndexError, TypeError):
            return None
        probs = {t["token"]: math.exp(t["logprob"]) for t in top if t["token"] in ("A", "B", "C")}
        total = sum(probs.values())
        return round(probs.get("A", 0) / total, 3) if total else None
