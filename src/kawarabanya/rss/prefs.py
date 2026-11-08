"""👍 と 👎 から学ぶ好み（まとめの並べ替え用）。

記事の特徴（ジャンル・サブジャンル・見出しと冒頭の名詞）と、記事を出したサイトのそれぞれに
    w = Σ(👍 − 👎) × 減衰 ÷ (その特徴が付いた投稿の数 × 減衰 + K)
を求める。投稿の数で割るのは、よく流れてくるジャンルが 👍 の数だけで勝たないようにするため。
K は、👍 が少ないうちは好みを 0 近くに留めるため。減衰は半減期 half_life_days 日で、好みの変化に追いつく。

記事の点 = ジャンル w + サブジャンル w + 名詞の w（絶対値の大きい 3 つの平均）→ ±pref_max で頭打ち、
それとは別にサイトの w → ±site_pref_max で頭打ち。
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from kawarabanya.core.llm import lead
from kawarabanya.rss import archive
from kawarabanya.rss.genres import normalize_word, top_genre

K = 5
# 「二回」「九回」のような漢数字＋助数詞（fugashi で名詞だけ取ると「交差点」が「交差」に割れるので、形態素解析は使わない）
NUMERAL_RE = re.compile(r"[〇一二三四五六七八九十百千万]+[回点人年月日時分秒本件個位勝敗度番打]")
WINDOW_DAYS = 180  # 半減期 60 日なら 180 日前の票は 1/8
TOP_WORDS = 3
# 投稿のこの割合以上に出る語（「発表」「公開」など）は、どの記事にもある語として外す
COMMON_DF = 0.05
COMMON_MIN = 10
# 名詞の代わり: カタカナ 3 字以上・漢字 2 字以上・英数字の語（形態素解析はしない）。
# 漢字は数字の直後から始まるもの（「第97回日本選手権」の「回日本選手権」、「2死満塁」の「死満塁」）を取らない
WORD_RE = re.compile(r"[ァ-ヺー]{3,}|(?<![0-9一-鿿々])[一-鿿々]{2,}|[0-9a-z][0-9a-z+#.\-]*[0-9a-z+#]")


@dataclass
class Prefs:
    """学んだ好み。words はジャンル（"#tech"）・サブジャンル（"#tech/ai"）・語 → w、sites は feed_id → w。"""
    words: dict[str, float] = field(default_factory=dict)
    sites: dict[int, float] = field(default_factory=dict)


def words(text: str) -> set[str]:
    """見出しや冒頭から名詞らしい語を取り出す（NFKC・小文字。数字だけ・2 字以下の英数字は除く）。"""
    return {
        w for w in WORD_RE.findall(normalize_word(text))
        if not (w.isascii() and (len(w) < 3 or not re.search("[a-z]", w))) and not NUMERAL_RE.fullmatch(w)
    }


def features(category: str | None, title: str | None, body: str | None) -> set[str]:
    """記事の特徴: ジャンル・サブジャンル（"#" 付き）と、見出しと冒頭の語。"""
    out = words(f"{title or ''}\n{lead(body or '')}")
    if g := top_genre(category):
        out.add(f"#{g}")
    if category and "/" in category:
        out.add(f"#{category}")
    return out


def item_features(row: sqlite3.Row) -> set[str]:
    """items の行（category・title・md_path）から特徴を作る。"""
    path = row["md_path"]
    body = archive.read_body(path) if path and Path(path).exists() else ""
    return features(row["category"], row["title"], body)


def learn(conn: sqlite3.Connection, now: datetime, half_life_days: float) -> Prefs:
    """直近 WINDOW_DAYS 日に投稿した記事の 👍・👎 から好みを求める。票がなければ空。"""
    rows = conn.execute(
        "SELECT i.feed_id, COALESCE(i.subgenre, f.category) AS category, i.title, i.md_path, i.notified_at,"
        " COALESCE(i.likes, 0) - COALESCE(i.dislikes, 0) AS votes FROM items i JOIN feeds f ON f.id = i.feed_id"
        " WHERE i.status = 'notified' AND i.notified_at >= ?",
        ((now - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds"),),
    ).fetchall()
    if not any(r["votes"] for r in rows):
        return Prefs()
    votes: dict[str, float] = defaultdict(float)
    seen: dict[str, float] = defaultdict(float)
    df: Counter[str] = Counter()
    site_votes: dict[int, float] = defaultdict(float)
    site_seen: dict[int, float] = defaultdict(float)
    for r in rows:
        age = (now - datetime.fromisoformat(r["notified_at"])).total_seconds() / 86400
        decay = 0.5 ** (max(age, 0) / half_life_days)
        site_votes[r["feed_id"]] += r["votes"] * decay
        site_seen[r["feed_id"]] += decay
        for f in item_features(r):
            votes[f] += r["votes"] * decay
            seen[f] += decay
            df[f] += 1
    common = max(COMMON_MIN, COMMON_DF * len(rows))
    return Prefs(
        {f: v / (seen[f] + K) for f, v in votes.items() if v and (f.startswith("#") or df[f] < common)},
        {s: v / (site_seen[s] + K) for s, v in site_votes.items() if v},
    )


def _clip(x: float, limit: float) -> float:
    return max(-limit, min(limit, x))


def bonus(prefs: Prefs, feed_id: int, feats: set[str], pref_max: float, site_max: float) -> float:
    """記事の好みの点（特徴の分は ±pref_max、サイトの分は ±site_max で頭打ち）。"""
    cats = sum(prefs.words.get(f, 0) for f in feats if f.startswith("#"))
    ws = sorted((prefs.words.get(f, 0) for f in feats if not f.startswith("#")), key=abs, reverse=True)[:TOP_WORDS]
    learned = cats + (sum(ws) / len(ws) if ws else 0)
    return _clip(learned, pref_max) + _clip(prefs.sites.get(feed_id, 0), site_max)
