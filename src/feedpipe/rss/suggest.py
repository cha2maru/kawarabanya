"""どのサブジャンルにも当てはまらなかった記事から、新しいサブジャンルを LLM に決めさせて genres.toml に足す。

当てはまらなかった記事は items.subgenre がトップレベルのジャンル（"news"）になっている（pipeline.classify_item）。
LLM には今のジャンル・サブジャンルの一覧と記事の見出しを渡し、思考モードで時間をかけて
「どのジャンルに、どんなサブジャンルを作り、どの記事が入るか」を JSON で答えさせる。

足すとジャンルのサブジャンルが Discord のタグの上限（20）を超えるときは、先に似たサブジャンルを
LLM にまとめさせる（統合）。まとめきれなければ、そのジャンルには足さない。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import db
from ..discord_api import MAX_TAGS
from ..llm import Summarizer
from .genres import Genre, section_re, set_key, top_genre

# この件数以上の記事がまとまるときだけ作る
MIN_ITEMS = 3
MAX_TITLES = 100
MAX_SUGGESTIONS = 3
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,19}$")

PROMPT = (
    "あなたはニュースサイトの編集者です。記事を分類するサブジャンルの一覧に、新しいサブジャンルを足します。\n"
    "下の記事は、今あるどのサブジャンルにも当てはまらなかったものです。よく読んで、"
    "{min_items} 件以上の記事がまとまって入る、はっきりした話題のサブジャンルを最大 {max_n} 個考えてください。\n"
    "- 今あるサブジャンルと重なるもの、「その他」「雑多」のような曖昧なものは作らない\n"
    "- genre は今あるジャンルのキーから選ぶ。key は英小文字の短い名前、label は日本語 10 字以内、"
    "description は判定の手がかりになる説明（30 字程度）\n"
    "- items には、そのサブジャンルに入る記事の番号を書く\n"
    "- まとまる話題がなければ空の一覧にする\n"
    '答えは JSON だけで: {{"subgenres": [{{"genre": "...", "key": "...", "label": "...", '
    '"description": "...", "items": [1, 2, 3]}}]}}'
)


@dataclass
class Suggestion:
    """新しいサブジャンルの案。"""
    genre: str
    key: str
    label: str
    description: str
    item_ids: list[int] = field(default_factory=list)

    @property
    def path(self) -> str:
        return f"{self.genre}/{self.key}"


def unmatched_items(conn: sqlite3.Connection, genres: dict[str, Genre], days: int) -> list[sqlite3.Row]:
    """直近 days 日の、どのサブジャンルにも当てはまらなかった記事（新しい順）。"""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    tops = [g.path for g in genres.values() if not g.is_sub]
    rows = conn.execute(
        "SELECT id, title, subgenre, status FROM items WHERE status IN ('saved', 'notified') AND fetched_at >= ?"
        f" AND subgenre IN ({','.join('?' * len(tops))}) ORDER BY fetched_at DESC",
        (since, *tops),
    ).fetchall()
    return rows[:MAX_TITLES]


def _prompt_input(genres: dict[str, Genre], rows: list[sqlite3.Row]) -> str:
    lines = ["# 今あるジャンルとサブジャンル"]
    for top in (g for g in genres.values() if not g.is_sub):
        subs = "、".join(g.label for g in genres.values() if g.is_sub and g.top == top.path)
        lines.append(f"- {top.path}（{top.label}）: {subs or 'なし'}")
    lines.append("\n# どのサブジャンルにも当てはまらなかった記事（番号: [ジャンル] 見出し）")
    lines += [f"{n}: [{r['subgenre']}] {r['title']}" for n, r in enumerate(rows, 1)]
    return "\n".join(lines)


def parse(text: str, genres: dict[str, Genre], rows: list[sqlite3.Row]) -> list[Suggestion]:
    """LLM の答えから、使える案だけを取り出す。

    ジャンルがない・キーや名前が既存と重なる・タグの上限を超える・記事が MIN_ITEMS 件に満たない案は捨てる。
    投稿済みの記事はフォーラムを移せないので、同じジャンルの記事だけを入れる。
    """
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        raw = json.loads(m.group(0))["subgenres"] if m else []
    except (ValueError, KeyError, TypeError):
        return []
    out: list[Suggestion] = []
    for s in raw if isinstance(raw, list) else []:
        try:
            genre, key = str(s["genre"]), str(s["key"]).lower()
            label, desc = str(s["label"]).strip()[:20], str(s.get("description", "")).strip()
            numbers = [int(n) for n in s["items"]]
        except (KeyError, TypeError, ValueError):
            continue
        if genre not in genres or genres[genre].is_sub or not KEY_RE.match(key) or not label:
            continue
        siblings = [g for g in genres.values() if g.is_sub and g.top == genre]
        siblings += [x for x in out if x.genre == genre]  # 同じ回の案どうしも重ならないように
        if f"{genre}/{key}" in genres or any(x.label == label for x in siblings):
            continue
        ids = [
            rows[n - 1]["id"] for n in dict.fromkeys(numbers)
            if 1 <= n <= len(rows) and (rows[n - 1]["status"] == "saved" or top_genre(rows[n - 1]["subgenre"]) == genre)
        ]
        if len(ids) >= MIN_ITEMS:
            out.append(Suggestion(genre, key, label, desc, ids))
    return out[:MAX_SUGGESTIONS]


async def suggest(
    conn: sqlite3.Connection, summarizer: Summarizer, genres: dict[str, Genre], days: int = 7
) -> list[Suggestion]:
    """当てはまらなかった記事から、新しいサブジャンルの案を作る。記事が少なければ LLM を呼ばない。"""
    rows = unmatched_items(conn, genres, days)
    if len(rows) < MIN_ITEMS:
        return []
    system = PROMPT.format(min_items=MIN_ITEMS, max_n=MAX_SUGGESTIONS)
    text = await summarizer.think(system, _prompt_input(genres, rows), "新しいサブジャンルの提案")
    return parse(text or "", genres, rows)


MERGE_PROMPT = (
    "あなたはニュースサイトの編集者です。ジャンル「{genre}」のサブジャンルが多すぎるので、"
    "似たもの・重なるものをまとめて、全部で {target} 個以下にします。\n"
    "- まとめるサブジャンルの key を keys に並べ、残す key（keys のどれか）と、まとめた後の label（日本語 10 字以内）・"
    "description（判定の手がかりになる説明。30 字程度）を書く\n"
    "- まとめないサブジャンルは書かない。「固定」と書いたものはまとめられない\n"
    '答えは JSON だけで: {{"merges": [{{"keys": ["a", "b"], "key": "a", "label": "...", "description": "..."}}]}}'
)


@dataclass
class Merge:
    """サブジャンルの統合の案（keys を key 1 つにまとめる）。"""
    genre: str
    keys: list[str]
    key: str
    label: str
    description: str

    @property
    def removed(self) -> list[str]:
        return [f"{self.genre}/{k}" for k in self.keys if k != self.key]


def _subs(genres: dict[str, Genre], genre: str) -> list[Genre]:
    return [g for g in genres.values() if g.is_sub and g.top == genre]


async def merge(
    summarizer: Summarizer, genres: dict[str, Genre], genre: str, adding: list[Suggestion], target: int
) -> list[Merge] | None:
    """genre のサブジャンル（adding を足した後）を target 個以下にまとめる案。まとめきれなければ None。

    フィードを固定したサブジャンル（feeds を書いたもの）はまとめない。
    """
    subs = [(g.path.split("/")[1], g.label, g.description, bool(g.feeds)) for g in _subs(genres, genre)]
    subs += [(s.key, s.label, s.description, False) for s in adding]
    lines = [f"- {k}: {label}（{desc}）" + ("【固定】" if fixed else "") for k, label, desc, fixed in subs]
    system = MERGE_PROMPT.format(genre=genres[genre].label, target=target)
    text = await summarizer.think(system, "\n".join(lines), f"サブジャンルの統合（{genre}）")
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        raw = json.loads(m.group(0))["merges"] if m else []
    except (ValueError, KeyError, TypeError):
        return None
    movable = {k for k, *_, fixed in subs if not fixed}
    used: set[str] = set()
    merges: list[Merge] = []
    for x in raw if isinstance(raw, list) else []:
        try:
            keys = list(dict.fromkeys(str(k) for k in x["keys"]))
            mg = Merge(genre, keys, str(x["key"]), str(x["label"]).strip()[:20], str(x.get("description", "")).strip())
        except (KeyError, TypeError, ValueError):
            continue
        if len(keys) >= 2 and mg.key in keys and set(keys) <= movable and not used & set(keys) and mg.label:
            used |= set(keys)
            merges.append(mg)
    left = len(subs) - sum(len(mg.keys) - 1 for mg in merges)
    return merges if left <= target else None


def apply(
    conn: sqlite3.Connection, genres_file: str | Path, suggestions: list[Suggestion], merges: list[Merge] = ()
) -> None:
    """統合の案と追加の案を genres.toml に書き、記事の items.subgenre を書き換える。"""
    path = Path(genres_file)
    text = path.read_text(encoding="utf-8")
    day = datetime.now().strftime("%Y-%m-%d")
    for mg in merges:
        for removed in mg.removed:
            text = section_re(removed).sub("", text, count=1)
        keep = f"{mg.genre}/{mg.key}"
        if m := section_re(keep).search(text):
            sec = set_key(set_key(m.group(0), "label", mg.label), "description", mg.description)
            text = text[: m.start()] + sec + text[m.end():]
        # 追加の案どうし・追加の案への統合は、追加の案を書き換える
        for s in suggestions:
            if f"{s.genre}/{s.key}" in mg.removed:
                s.key = mg.key
            if f"{s.genre}/{s.key}" == keep:
                s.label, s.description = mg.label, mg.description
    added: set[str] = set()
    for s in suggestions:
        if s.path in added or section_re(s.path).search(text):
            continue  # 既存のサブジャンルに統合された
        added.add(s.path)
        text = text.rstrip("\n") + (
            f"\n\n[{s.genre}.{s.key}]  # {day} feedpipe genres suggest が追加\n"
            f"label = {json.dumps(s.label, ensure_ascii=False)}\n"
            f"description = {json.dumps(s.description, ensure_ascii=False)}\n"
        )
    path.write_text(text, encoding="utf-8")
    with conn:
        for mg in merges:
            for removed in mg.removed:
                conn.execute("UPDATE items SET subgenre = ? WHERE subgenre = ?", (f"{mg.genre}/{mg.key}", removed))
        for s in suggestions:
            for item_id in s.item_ids:
                db.update_item(conn, item_id, subgenre=s.path)


async def plan_merges(
    summarizer: Summarizer, genres: dict[str, Genre], suggestions: list[Suggestion]
) -> tuple[list[Suggestion], list[Merge]]:
    """追加するとタグの上限を超えるジャンルは、似たサブジャンルをまとめる案を作る。まとめきれなければ足さない。"""
    keep: list[Suggestion] = []
    merges: list[Merge] = []
    for genre in dict.fromkeys(s.genre for s in suggestions):
        adding = [s for s in suggestions if s.genre == genre]
        if len(_subs(genres, genre)) + len(adding) <= MAX_TAGS:
            keep += adding
        elif (found := await merge(summarizer, genres, genre, adding, MAX_TAGS - 1)) is not None:
            keep += adding
            merges += found
    return keep, merges
