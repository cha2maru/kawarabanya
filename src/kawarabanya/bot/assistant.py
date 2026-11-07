"""Bot へのメンション（日本語の依頼）を、ローカル LLM で kawarabanya のコマンドに直す。

LLM には JSON Schema の文法制約を付けて、下の COMMANDS のどれか 1 つと引数だけを答えさせる。
引数の確かめ（ジャンルがあるか・フィードがどれか）と、コマンドの組み立てはコードで行う。
変更するコマンドは Bot がボタンで確認してから実行する（LLM の読み違いで勝手に変わらないように）。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from kawarabanya.core import db
from kawarabanya.core.llm import Summarizer
from kawarabanya.rss.genres import INTEREST_LEVELS, Genre

# コマンド名 → (説明, 実行前に確認するか)
COMMANDS: dict[str, tuple[str, bool]] = {
    "feed_add": ("サイト・フィードを追加する（url と genre）", True),
    "feed_remove": ("サイト・フィードを外す（feed にサイト名か URL）", True),
    "feed_list": ("登録しているサイトの一覧（genre で絞れる）", False),
    "genre_interest": ("ジャンル・サブジャンルの好き嫌いを変える（genre と level）", True),
    "genre_list": ("ジャンル・サブジャンルの一覧", False),
    "run_now": ("今すぐ記事を取り込む", True),
    "digest_now": ("今すぐまとめを投稿する（genre で絞れる）", True),
    "status": ("Bot の状態（最後の取り込み・次のまとめ・失敗中のフィードなど）", False),
    "interests": ("👍 から見た興味の傾向", False),
    "help": ("できることの一覧", False),
    "unknown": ("どれにも当てはまらない", False),
}

PROMPT = (
    "あなたは Discord の Bot です。ユーザーの依頼を、次のコマンドのどれか 1 つに直します。\n"
    "{commands}\n"
    "ジャンル（genre に書けるもの。「ジャンル / サブジャンル」の名前で言われたら左のキーにする）:\n"
    "{genres}\n"
    "level: like = 好き・もっと見たい / normal = 普通に戻す / low = 興味がうすい・まとめに出さない / "
    "mute = 要らない・投稿もしない\n"
    "- サイト名を言わずに「○○の記事」の好き嫌いを言われたら、feed_remove ではなく genre_interest にする\n"
    "- feed にはサイト名か URL だけを書く（ジャンルの名前は書かない）\n"
    "使わない引数は空文字にする。"
)


@dataclass
class Intent:
    """依頼を直した結果。"""
    command: str
    argv: list[str] = field(default_factory=list)  # kawarabanya に渡す引数（help・unknown は空）
    summary: str = ""  # 確認・返事に使う日本語の説明
    confirm: bool = False
    error: str | None = None  # 引数が足りない・見つからないとき


def _schema(genres: dict[str, Genre]) -> dict:
    return {
        "type": "object",
        "properties": {
            "command": {"enum": list(COMMANDS)},
            "url": {"type": "string"},
            "genre": {"enum": ["", *genres]},
            "feed": {"type": "string"},
            "level": {"enum": ["", *INTEREST_LEVELS]},
        },
        "required": ["command", "url", "genre", "feed", "level"],
    }


async def interpret(summarizer: Summarizer, text: str, genres: dict[str, Genre]) -> dict | None:
    """依頼の文を LLM でコマンドと引数（dict）に直す。失敗したら None。"""
    label = {g.path: g.label for g in genres.values() if not g.is_sub}
    lines = [f"- {p}: {label.get(g.top, g.top) + ' / ' if g.is_sub else ''}{g.label}" for p, g in genres.items()]
    system = PROMPT.format(
        commands="\n".join(f"- {name}: {desc}" for name, (desc, _) in COMMANDS.items()), genres="\n".join(lines)
    )
    return await summarizer.ask_json(system, text, _schema(genres), "依頼の解釈", max_tokens=200, timeout=120)


def find_feeds(conn: sqlite3.Connection, query: str) -> list[db.Feed]:
    """サイト名か URL の一部に当たる有効なフィード。"""
    q = query.strip().lower()
    if not q:
        return []
    feeds = db.list_feeds(conn, enabled=True)
    exact = [f for f in feeds if q in ((f.title or "").lower(), f.url.lower(), f.original_url.lower())]
    return exact or [f for f in feeds if q in (f.title or "").lower() or q in f.url.lower()]


def build(data: dict | None, text: str, conn: sqlite3.Connection, genres: dict[str, Genre]) -> Intent:
    """LLM の答えを確かめて、実行するコマンドにする。"""
    command = (data or {}).get("command") or "unknown"
    if command not in COMMANDS:
        command = "unknown"
    confirm = COMMANDS[command][1]
    genre = (data or {}).get("genre") or ""
    label = genres[genre].label if genre in genres else genre
    # URL は LLM に写させず、依頼の文から取る（長い URL を書き間違えることがあるため）
    # 日本語の文では URL の後ろに空白がないので、URL に使える ASCII の文字だけを拾う
    url = m.group(0).rstrip(">).,") if (m := re.search(r"https?://[!-~]+", text)) else ""
    match command:
        case "feed_add":
            if not url:
                return Intent(command, error="追加するサイトの URL を書いてください")
            argv = ["feeds", "add", url] + (["--genre", genre] if genre else [])
            return Intent(command, argv, f"{url} を{f'「{label}」に' if genre else 'ジャンルなしで'}追加", confirm)
        case "feed_remove":
            query = url or (data or {}).get("feed") or ""
            found = find_feeds(conn, query)
            # 「VAPE の記事はもういらない」をサイトの削除と読むことがあるので、ジャンルの名前ならミュートにする
            target = genre or next((p for p, g in genres.items() if query and query in (p, g.label)), "")
            if not found and target:
                return Intent("genre_interest", ["genres", "interest", target, "mute"],
                              f"「{genres[target].label}」をミュート（投稿もしない）にする", True)
            if len(found) != 1:
                names = "、".join(f"{f.title or f.url}" for f in found[:5])
                return Intent(command, error=f"「{query}」に当たるサイトが{f' {len(found)} 件あります: {names}' if found else '見つかりません'}")
            f = found[0]
            return Intent(command, ["feeds", "remove", str(f.id)], f"{f.title or f.url}（{f.category or '-'}）を外す", confirm)
        case "feed_list":
            return Intent(command, ["feeds", "list"] + (["--category", genre] if genre else []), "サイトの一覧")
        case "genre_interest":
            level = (data or {}).get("level") or ""
            if not genre or not level:
                return Intent(command, error="ジャンルと好き嫌い（好き・普通・うすい・ミュート）を書いてください")
            return Intent(command, ["genres", "interest", genre, level], f"「{label}」を{INTEREST_LEVELS[level]}にする", confirm)
        case "genre_list":
            return Intent(command, ["genres", "list"], "ジャンルの一覧")
        case "run_now":
            return Intent(command, ["run", "-q"], "今すぐ記事を取り込む", confirm)
        case "digest_now":
            return Intent(command, ["digest"] + (["--genre", genre] if genre else []),
                          f"今すぐ{f'「{label}」の' if genre else ''}まとめを投稿する", confirm)
        case "status":
            return Intent(command, ["status"], "状態")
        case "interests":
            return Intent(command, ["interests"], "興味の傾向")
    return Intent(command, summary=help_text() if command == "help" else "うまく読み取れませんでした。\n" + help_text())


def help_text() -> str:
    """できることの一覧。"""
    examples = [
        "https://example.com/news をボードゲームに追加して",
        "GIGAZINE を外して",
        "スポーツはまとめに出さなくていい",
        "今すぐまとめを出して",
        "いまどうなってる？",
    ]
    return "こんなふうに頼めます:\n" + "\n".join(f"・{e}" for e in examples)
