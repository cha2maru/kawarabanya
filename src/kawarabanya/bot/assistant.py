"""Bot へのメンション（日本語の依頼）を、ローカル LLM で kawarabanya のコマンドに直す。

LLM には JSON Schema の文法制約を付けて、下の COMMANDS のどれか 1 つと引数だけを答えさせる。
引数の確かめ（ジャンルがあるか・フィードがどれか）と、コマンドの組み立てはコードで行う。
変更するコマンドは Bot がボタンで確認してから実行する（LLM の読み違いで勝手に変わらないように）。
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

from kawarabanya.core import db
from kawarabanya.core.llm import Summarizer
from kawarabanya.rss.genres import INTEREST_LEVELS, Genre, has_word, normalize_word

# コマンド名 → (説明, 実行前に確認するか)
COMMANDS: dict[str, tuple[str, bool]] = {
    "feed_add": ("サイト・フィードを追加する（url と genre）", True),
    "feed_remove": ("サイト・フィードを外す（feed にサイト名か URL）", True),
    "feed_list": ("登録しているサイトの一覧（genre で絞れる）", False),
    "opml_import": ("添付された OPML ファイルのサイトをまとめて追加する（genre で入れるジャンルを決められる）", True),
    "opml_export": ("登録しているサイトを OPML ファイルにして渡す", False),
    "genre_interest": ("ジャンル・サブジャンルの好き嫌いを変える（genre と level）", True),
    "feed_interest": ("特定のサイトの好き嫌いを変える（feed にサイト名、level）", True),
    "genre_assign": ("記事や単語のジャンルを指定する・直す（genre に行き先、word に単語。単語がなければ空）", True),
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
    "level: like = 好き・もっと見たい / normal = 普通に戻す / down = 優先度を下げる・まとめの下の方に / "
    "low = 興味がうすい・まとめに出さない / mute = 要らない・投稿もしない\n"
    "- サイト名を挙げて好き嫌い・優先度を言われたら feed_interest、ジャンルなら genre_interest\n"
    "- サイト名を言わずに「○○の記事」の好き嫌いを言われたら、feed_remove ではなく genre_interest にする\n"
    "- feed にはサイト名か URL だけを書く（ジャンルの名前は書かない）\n"
    "- 「○○は××」「○○の記事は××に」のように単語のジャンルを言われたら genre_assign（word に ○○、genre に ××）\n"
    "- 「これは××」「××に移して」は genre_assign（word は空）\n"
    "- word には依頼に書かれた固有名詞だけをそのまま書く\n"
    "使わない引数は空文字にする。"
)
# 「これは映画」の「これ」は記事を指すので、振り分けの語にしない
NOT_WORDS = {"これ", "それ", "この記事", "この投稿", "記事", "投稿"}


@dataclass
class Intent:
    """依頼を直した結果。"""
    command: str
    argv: list[str] = field(default_factory=list)  # kawarabanya に渡す引数（help・unknown は空）
    summary: str = ""  # 確認・返事に使う日本語の説明
    confirm: bool = False
    error: str | None = None  # 引数が足りない・見つからないとき
    attach: str | None = None  # 実行後に返事に添付するファイル（OPML の書き出し）


def _schema(genres: dict[str, Genre]) -> dict:
    return {
        "type": "object",
        "properties": {
            "command": {"enum": list(COMMANDS)},
            "url": {"type": "string"},
            "genre": {"enum": ["", *genres]},
            "feed": {"type": "string"},
            "level": {"enum": ["", *INTEREST_LEVELS]},
            "word": {"type": "string"},
        },
        "required": ["command", "url", "genre", "feed", "level", "word"],
    }


async def interpret(
    summarizer: Summarizer,
    text: str,
    genres: dict[str, Genre],
    attachment_name: str | None = None,
    post_title: str | None = None,
) -> dict | None:
    """依頼の文を LLM でコマンドと引数（dict）に直す。post_title は依頼を書いた投稿の記事。失敗したら None。"""
    label = {g.path: g.label for g in genres.values() if not g.is_sub}
    lines = [f"- {p}: {label.get(g.top, g.top) + ' / ' if g.is_sub else ''}{g.label}" for p, g in genres.items()]
    system = PROMPT.format(
        commands="\n".join(f"- {name}: {desc}" for name, (desc, _) in COMMANDS.items()), genres="\n".join(lines)
    )
    if attachment_name:
        text += f"\n（ファイル {attachment_name} が添付されています）"
    if post_title:
        text += f"\n（この依頼は記事「{post_title}」の投稿への返信です）"
    return await summarizer.ask_json(system, text, _schema(genres), "依頼の解釈", max_tokens=200, timeout=120)


def find_feeds(conn: sqlite3.Connection, query: str) -> list[db.Feed]:
    """サイト名か URL の一部に当たる有効なフィード。"""
    q = query.strip().lower()
    if not q:
        return []
    feeds = db.list_feeds(conn, enabled=True)
    exact = [f for f in feeds if q in ((f.title or "").lower(), f.url.lower(), f.original_url.lower())]
    return exact or [f for f in feeds if q in (f.title or "").lower() or q in f.url.lower()]


def find_post(conn: sqlite3.Connection, thread_id: int) -> sqlite3.Row | None:
    """スレッド（フォーラムの投稿）の記事。まとめのスレッドなど、記事の投稿でなければ None。"""
    return conn.execute(
        "SELECT i.id, i.title, COALESCE(i.subgenre, f.category) AS category"
        " FROM items i JOIN feeds f ON f.id = i.feed_id WHERE i.discord_thread_id = ?",
        (str(thread_id),),
    ).fetchone()


def _named_genre(text: str, genres: dict[str, Genre]) -> str | None:
    """依頼の文にそのまま書かれたジャンルのキーか名前（複数なら長いもの）。"""
    named = [(len(n), p) for p, g in genres.items() for n in (p, g.label) if has_word(text, n)]
    return max(named)[1] if named else None


def _word(data: dict | None, text: str, genre: Genre) -> str:
    """LLM が挙げた振り分けの語。依頼の文にない語や、ジャンルの名前そのものは使わない。"""
    word = unicodedata.normalize("NFKC", (data or {}).get("word") or "").strip()
    names = {normalize_word(n) for n in (genre.label, genre.top, genre.path.split("/")[-1])}
    if len(word) < 2 or word in NOT_WORDS or normalize_word(word) in names or normalize_word(word) not in normalize_word(text):
        return ""
    return word


def build(
    data: dict | None,
    text: str,
    conn: sqlite3.Connection,
    genres: dict[str, Genre],
    attachment: str | None = None,
    export_path: str = "data/exports/kawarabanya.opml",
    post: sqlite3.Row | None = None,
) -> Intent:
    """LLM の答えを確かめて、実行するコマンドにする。

    attachment は依頼に添付された OPML を保存したパス。post は依頼を書いた投稿の記事（find_post）。
    """
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
            # ジャンルを言われなければ LLM に決めさせる（フィードの最近の見出しから）
            argv = ["feeds", "add", url, "--genre", genre or "auto"]
            return Intent(command, argv, f"{url} を{f'「{label}」に' if genre else '（ジャンルは自動で決めて）'}追加", confirm)
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
        case "opml_import":
            if not attachment:
                return Intent(command, error="取り込む OPML ファイルを添付して頼んでください")
            argv = ["import-opml", attachment, "--genre", genre or "auto"]
            return Intent(command, argv, f"添付の OPML を{f'「{label}」に' if genre else '（ジャンルはフォルダ名か自動で決めて）'}取り込む", confirm)
        case "opml_export":
            return Intent(command, ["export-opml", export_path], "登録しているサイトの OPML", attach=export_path)
        case "feed_list":
            return Intent(command, ["feeds", "list"] + (["--category", genre] if genre else []), "サイトの一覧")
        case "genre_interest":
            level = (data or {}).get("level") or ""
            if not genre or not level:
                return Intent(command, error="ジャンルと好き嫌い（好き・普通・うすい・ミュート）を書いてください")
            return Intent(command, ["genres", "interest", genre, level], f"「{label}」を{INTEREST_LEVELS[level]}にする", confirm)
        case "feed_interest":
            level = (data or {}).get("level") or ""
            query = (data or {}).get("feed") or ""
            found = find_feeds(conn, query)
            if not level or len(found) != 1:
                names = "、".join(f.title or f.url for f in found[:5])
                return Intent(command, error=f"サイト（{query or '?'}）と好き嫌いを 1 つに決められません" + (f": {names}" if names else ""))
            f = found[0]
            # normal は設定を消す（feeds option で値を省くと削除）
            argv = ["feeds", "option", str(f.id), "interest"] + ([] if level == "normal" else [level])
            return Intent(command, argv, f"「{f.title or f.url}」を{INTEREST_LEVELS[level]}にする", confirm)
        case "genre_assign":
            # 「これはculture」を LLM が culture/comic と読むことがあるので、書かれた名前を優先する
            genre = _named_genre(text, genres) or genre
            label = genres[genre].label if genre in genres else genre
            if not genre:
                return Intent(command, error="行き先のジャンルを書いてください")
            word = _word(data, text, genres[genre])
            also = f"（「{word}」の記事も今後はここへ）" if word else ""
            if post:
                note = "" if post["category"].split("/")[0] == genres[genre].top else "。元の投稿は消え、👍 は引き継げません"
                argv = ["items", "move", str(post["id"]), genre] + (["--keyword", word] if word else [])
                return Intent(command, argv, f"「{post['title']}」を「{label}」に移す{also}{note}", confirm)
            if not word:
                return Intent(command, error="記事の投稿のスレッドで頼むか、「Cloudflareはtech」のように単語とジャンルを書いてください")
            return Intent(command, ["genres", "keyword", word, genre], f"「{word}」の記事を今後は「{label}」に振り分ける", confirm)
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
        "Cloudflareはtech",
        "（記事の投稿のスレッドで）これは映画",
        "（OPML を添付して）これ取り込んで",
        "OPML ちょうだい",
        "今すぐまとめを出して",
        "いまどうなってる？",
    ]
    return "こんなふうに頼めます:\n" + "\n".join(f"・{e}" for e in examples)
