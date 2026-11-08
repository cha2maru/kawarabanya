"""世間での話題になり方（まとめの並べ替え用）。

- Google トレンドの急上昇ワード: 公開の RSS は最新の 10 件ほどしか返さないので、run のたびに取って 24 時間ためる。
  見出しか冒頭にワードがある記事に、検索数に応じて加点する。
- 同じ出来事を報じたサイト数: 重複（duplicate）にした記事の error に残る元の記事の ID から数える。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx

from kawarabanya.core import db
from kawarabanya.rss.genres import has_word

URL = "https://trends.google.com/trending/rss?geo={geo}"
NS = {"ht": "https://trends.google.com/trending/rss"}
KEEP_HOURS = 24
# 加点の上限（好き嫌いの補正 LIKE_BONUS と同じ。話題でも好き嫌いを超えて上げない）
MAX_BONUS = 0.3


def parse(xml: bytes) -> dict[str, int]:
    """RSS から 急上昇ワード → 検索数の目安（"2000+" → 2000）。"""
    out: dict[str, int] = {}
    for item in ET.fromstring(xml).iter("item"):
        query = (item.findtext("title") or "").strip()
        digits = re.sub(r"\D", "", item.findtext("ht:approx_traffic", "", NS))
        # 「sg」のような短い英字は、商品名などの一部に当たって誤る
        if query and not (query.isascii() and len(query) <= 2):
            out[query] = max(out.get(query, 0), int(digits or 100))
    return out


def record(conn: sqlite3.Connection, words: dict[str, int], now: datetime) -> dict[str, list]:
    """ワードを state にため（同じワードは大きい検索数と新しい時刻）、24 時間より古いものを消す。"""
    kept = load_raw(conn)
    for q, traffic in words.items():
        kept[q] = [max(traffic, kept.get(q, [0])[0]), now.isoformat(timespec="seconds")]
    limit = now - timedelta(hours=KEEP_HOURS)
    kept = {q: v for q, v in kept.items() if datetime.fromisoformat(v[1]) >= limit}
    db.set_state(conn, "trends", json.dumps(kept, ensure_ascii=False))
    return kept


def load_raw(conn: sqlite3.Connection) -> dict[str, list]:
    """ためたワード（ワード → [検索数, 最後に見た時刻]）。"""
    return json.loads(db.get_state(conn, "trends") or "{}")


def fetch(conn: sqlite3.Connection, geo: str, user_agent: str, timeout: float = 30,
          transport: httpx.BaseTransport | None = None) -> int:
    """急上昇ワードを取ってためる。ためているワードの数を返す。"""
    with httpx.Client(headers={"User-Agent": user_agent}, timeout=timeout, transport=transport) as client:
        resp = client.get(URL.format(geo=geo))
        resp.raise_for_status()
    return len(record(conn, parse(resp.content), datetime.now(UTC)))


def load(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    """直近 24 時間の急上昇ワード → 検索数の目安。"""
    limit = now - timedelta(hours=KEEP_HOURS)
    return {q: v[0] for q, v in load_raw(conn).items() if datetime.fromisoformat(v[1]) >= limit}


def trend_bonus(text: str, words: dict[str, int]) -> float:
    """text（見出しと冒頭）に当たる急上昇ワードの加点（100+ で 0.1、1000+ で 0.2、1 万+ で 0.3）。"""
    hits = [t for q, t in words.items() if all(has_word(text, w) for w in q.split())]
    return min(MAX_BONUS, max(0.1 * math.log10(max(t, 10) / 10) for t in hits)) if hits else 0.0


def source_counts(conn: sqlite3.Connection, since: datetime) -> dict[int, int]:
    """記事 ID → 同じ記事・同じ出来事を報じたサイトの数（元の記事のサイトを含む。重複がなければ載らない）。"""
    feeds: dict[int, set[int]] = defaultdict(set)
    rows = conn.execute(
        "SELECT feed_id, error FROM items WHERE status = 'duplicate' AND fetched_at >= ?",
        (since.isoformat(timespec="seconds"),),
    )
    for feed_id, error in rows:
        if m := re.search(r"item (\d+)", error or ""):
            feeds[int(m.group(1))].add(feed_id)
    if not feeds:
        return {}
    own = dict(conn.execute(f"SELECT id, feed_id FROM items WHERE id IN ({','.join('?' * len(feeds))})", list(feeds)))
    return {i: len(fs | {own[i]}) for i, fs in feeds.items() if i in own}


def source_bonus(sources: int) -> float:
    """報じたサイトが 1 つ増えるごとに 0.1（上限 MAX_BONUS）。"""
    return min(MAX_BONUS, 0.1 * max(sources - 1, 0))
