"""👍 と 👎 から学ぶ好み（まとめの並べ替え用）。

記事の特徴（ジャンル・サブジャンル・見出しと冒頭の名詞）と、記事を出したサイトのそれぞれに
    w = Σ(👍 − 👎) × 減衰 ÷ (その特徴が付いた投稿の数 × 減衰 + K)
を求める。投稿の数で割るのは、よく流れてくるジャンルが 👍 の数だけで勝たないようにするため。
K は、👍 が少ないうちは好みを 0 近くに留めるため。減衰は半減期 half_life_days 日で、好みの変化に追いつく。

記事の点 = ジャンル w + サブジャンル w + 名詞の w（絶対値の大きい 3 つの平均）→ ±pref_max で頭打ち、
それとは別にサイトの w → ±site_pref_max で頭打ち。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from kawarabanya.core import db
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


# 語の見直し（prefs review）: 好みに使わない語・確認済みの語・前回の見直しの日時を state に置く
IGNORE_KEY = "prefs_ignore"
REVIEWED_KEY = "prefs_reviewed"
REVIEWED_AT_KEY = "prefs_reviewed_at"
REVIEW_DAYS = 7


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
    ignored = _load(conn, IGNORE_KEY)
    return Prefs(
        {f: v / (seen[f] + K) for f, v in votes.items() if v and (f.startswith("#") or (df[f] < common and f not in ignored))},
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


def _load(conn: sqlite3.Connection, key: str) -> set[str]:
    return set(json.loads(db.get_state(conn, key) or "[]"))


def _save(conn: sqlite3.Connection, key: str, values: set[str]) -> None:
    db.set_state(conn, key, json.dumps(sorted(values), ensure_ascii=False))


def ignored_words(conn: sqlite3.Connection) -> set[str]:
    """好みに使わない語。"""
    return _load(conn, IGNORE_KEY)


def ignore(conn: sqlite3.Connection, ws: list[str], *, remove: bool = False) -> list[str]:
    """語を好みに使わない語に足す（remove なら戻す）。NFKC・小文字にそろえた語を返す。"""
    norm = [normalize_word(w).strip() for w in ws if w.strip()]
    current = _load(conn, IGNORE_KEY)
    _save(conn, IGNORE_KEY, current - set(norm) if remove else current | set(norm))
    return norm


def to_review(conn: sqlite3.Connection, learned: Prefs, n: int) -> list[tuple[str, float]]:
    """まだ確認していない語のうち、好みが最も + の n 語と最も - の n 語（ジャンルは除く）。"""
    reviewed = _load(conn, REVIEWED_KEY)
    ws = sorted(((w, v) for w, v in learned.words.items() if not w.startswith("#") and w not in reviewed), key=lambda x: -x[1])
    return [*(x for x in ws[:n] if x[1] > 0), *(x for x in ws[::-1][:n] if x[1] < 0)]


def mark_reviewed(conn: sqlite3.Connection, ws: list[str], now: datetime) -> None:
    """語を確認済みにし、見直しの日時を記録する。"""
    _save(conn, REVIEWED_KEY, _load(conn, REVIEWED_KEY) | set(ws))
    db.set_state(conn, REVIEWED_AT_KEY, now.isoformat(timespec="seconds"))


def last_review(conn: sqlite3.Connection) -> datetime | None:
    """前回の見直しの日時（まだなら None）。"""
    at = db.get_state(conn, REVIEWED_AT_KEY)
    return datetime.fromisoformat(at) if at else None
