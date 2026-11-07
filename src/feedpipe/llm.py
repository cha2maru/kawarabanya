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
CLASSIFY_PROMPT = (
    "あなたはニュースの編集者です。記事の見出しと冒頭を読み、最も当てはまる分類を 1 つ選びます。\n"
    "{choices}\n"
    "{letters} のいずれか 1 文字だけで答えてください。"
)
# 選択肢の記号（全ジャンルのサブジャンルから選ぶので 51 個まで）
CHOICE_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXY" + "abcdefghijklmnopqrstuvwxyz"
NONE_LETTER = "Z"
SAME_STORY_PROMPT = (
    "2 つのニュース記事が、同じ出来事（同じ事件・発表・話題）を報じたものかを判定します。"
    "同じなら A、関連はあっても別の出来事なら B を 1 文字だけ答えてください。"
)
TREND_PROMPT = (
    "あなたはニュース編集者です。今日のニュース見出しの一覧を読み、"
    "全体として目立つ傾向を日本語2文以内で述べてください。一覧にないことは書かないでください。"
)

CJK = r"぀-ヿ㐀-鿿！-｠"


def prepare_input(title: str | None, markdown: str, max_chars: int) -> str:
    """画像・リンク先 URL を除いて文字数を減らし、先頭 max_chars 字に切る。"""
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", markdown)
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()[:max_chars]
    return f"タイトル: {title or '(無題)'}\n\n本文:\n{body}"


def _headline(title: str | None, markdown: str | None) -> str:
    return f"見出し: {title or '(無題)'}\n冒頭: {lead(markdown or '')}"


def lead(markdown: str, chars: int = LEAD_CHARS) -> str:
    """本文の冒頭を、画像・リンク先 URL・改行を除いたプレーンテキストで返す。"""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", markdown)
    return re.sub(r"\s+", " ", text).strip()[:chars]


def clean_output(text: str) -> str:
    """LLM の出力から <think> ブロックと、日本語と英数字の間の余分な空白を除く。"""
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

    async def _chat(self, body: dict, what: str, timeout: float | None = None) -> dict | None:
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
                resp = await self._client.post("/chat/completions", json=body, timeout=timeout or self.cfg.timeout)
                resp.raise_for_status()
                return resp.json()
            except httpx.ConnectError as e:
                log.warning("要約サーバーに接続できないため、今回は要約・判定しません: %s", e)
                self.available = False
            except (httpx.HTTPError, ValueError) as e:
                log.warning("%sに失敗: %s", what, e)
        return None

    async def summarize(self, title: str | None, markdown: str | None) -> str | None:
        """本文の先頭 max_input_chars 字から 2〜3 文の要約を作る。失敗したら None。"""
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

    async def think(self, system: str, user: str, what: str, max_tokens: int = 8192, timeout: float = 1800) -> str | None:
        """思考モードで時間をかけて答えさせ、答え（思考を除く）を返す。CPU では数分〜十数分かかる。"""
        data = await self._chat(
            {
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.6,
                "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": True},
            },
            what,
            timeout,
        )
        try:
            return re.sub(r"<think>.*?</think>", "", data["choices"][0]["message"]["content"], flags=re.DOTALL).strip()
        except (KeyError, IndexError, TypeError):
            return None

    async def trend(self, titles: list[str]) -> str | None:
        """見出しの一覧から、全体の傾向を 2 文で書かせる。"""
        if not titles:
            return None
        data = await self._chat(
            {
                "messages": [
                    {"role": "system", "content": TREND_PROMPT},
                    {"role": "user", "content": "\n".join(f"- {t}" for t in titles)},
                ],
                "temperature": self.cfg.temperature,
                "max_tokens": 200,
            },
            "傾向のまとめ",
        )
        if data is None:
            return None
        try:
            return clean_output(data["choices"][0]["message"]["content"]) or None
        except (KeyError, IndexError, TypeError):
            return None

    async def _choose(self, system: str, user: str, letters: str, what: str) -> dict[str, float] | None:
        """user（見出しと冒頭など）を渡して letters のどれか 1 文字だけを出させ、各文字の確率（合計 1）を返す。

        文章を書かせず 1 トークンだけを出させ、その確率を読む（Jev 風の判定）。
        出力が 1 トークンなので、時間はほぼ入力の読み込みだけ（CPU で 1〜2 秒）。
        """
        data = await self._chat(
            {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "max_tokens": 1,
                "logprobs": True,
                "top_logprobs": 20,
                # llama-server の文法制約で、選択肢の 1 文字に限る
                "grammar": "root ::= " + " | ".join(f'"{c}"' for c in letters),
            },
            what,
        )
        try:
            top = data["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        except (KeyError, IndexError, TypeError):
            return None
        probs = {t["token"]: math.exp(t["logprob"]) for t in top if t["token"] in letters}
        total = sum(probs.values())
        return {k: v / total for k, v in probs.items()} if total else None

    async def priority(self, title: str | None, markdown: str | None, criteria: str | None = None) -> float | None:
        """見出しと冒頭から重要度を判定し、「A（高い）」の確率（0〜1）を返す。"""
        system = PRIORITY_PROMPT.format(criteria=criteria or DEFAULT_CRITERIA)
        probs = await self._choose(system, _headline(title, markdown), "ABC", f"重要度の判定（{title}）")
        return round(probs.get("A", 0), 3) if probs else None

    async def same_story(
        self, a: tuple[str | None, str | None], b: tuple[str | None, str | None]
    ) -> float | None:
        """2 つの記事（(見出し, 本文)）が同じ出来事を報じたものである確率（0〜1）。判定に失敗したら None。"""
        user = f"記事1: {a[0] or '(無題)'}\n{lead(a[1] or '', 300)}\n\n記事2: {b[0] or '(無題)'}\n{lead(b[1] or '', 300)}"
        probs = await self._choose(SAME_STORY_PROMPT, user, "AB", f"同じ出来事の判定（{a[0]}）")
        return round(probs.get("A", 0), 3) if probs else None

    async def classify(
        self, title: str | None, markdown: str | None, options: list[tuple[str, str]]
    ) -> tuple[int | None, float] | None:
        """記事が options（(名前, 説明) の並び）のどれに当てはまるかを選ばせる。

        (選んだ番号, その確率) を返す。「どれにも当てはまらない」を選んだら番号は None。判定に失敗したら None。
        """
        letters = CHOICE_LETTERS[: len(options)] + NONE_LETTER
        lines = [f"{c}: {name}" + (f"（{desc}）" if desc else "") for c, (name, desc) in zip(letters, options)]
        lines.append(f"{NONE_LETTER}: どれにも当てはまらない")
        system = CLASSIFY_PROMPT.format(choices="\n".join(lines), letters="・".join(letters))
        probs = await self._choose(system, _headline(title, markdown), letters, f"サブジャンルの判定（{title}）")
        if not probs:
            return None
        best = max(probs, key=probs.get)
        return (None if best == NONE_LETTER else letters.index(best)), round(probs[best], 3)
