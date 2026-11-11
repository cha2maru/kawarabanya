import asyncio

import pytest

from kawarabanya.core import db
from kawarabanya.core.config import Config
from kawarabanya.core.llm import is_foreign, split_paragraphs
from kawarabanya.rss import notify, pipeline


def test_is_foreign_and_split():
    assert is_foreign("Three new apps built by Googlers", "Support our coverage.")
    assert is_foreign("谷歌发布新应用", "这是一个测试")  # 中国語（かながない）
    assert not is_foreign("Claude Code の新機能", "")
    assert not is_foreign("zed-industries/zed", "高速なエディタです")
    assert not is_foreign(None, None)
    assert split_paragraphs("a\n\nbb\n\n\n\nccc", 5) == ["a\n\nbb", "ccc"]


class FakeLLM:
    available = True

    def __init__(self):
        self.translated = []

    async def classify(self, *a):
        return None

    async def priority(self, *a):
        return 0.1  # 基準（0.8）未満

    async def summarize(self, title, md):
        return "日本語の要約"

    async def translate(self, text, title=False):
        self.translated.append(text)
        return f"訳:{text[:10]}"


def item(conn, tmp_path, title, body):
    fid = db.insert_feed(conn, "https://a.example/rss", "A", None, "tech")
    md = tmp_path / "a.md"
    md.write_text(f"---\ntitle: x\n---\n# {title}\n\n{body}\n", encoding="utf-8")
    conn.execute("INSERT INTO items (id, feed_id, key, title, fetched_at, status, md_path)"
                 " VALUES (1, ?, 'k', ?, 'x', 'notified', ?)", (fid, title, str(md)))
    conn.execute("INSERT INTO posts (item_id, platform, ref, created_at) VALUES (1, 'discord', '55', 'x')")
    return md


def test_foreign_article_is_summarized_and_title_translated(tmp_path):
    conn = db.connect(":memory:")
    md = item(conn, tmp_path, "Three new apps built by Googlers", "Support our coverage.")
    llm = FakeLLM()
    result = asyncio.run(pipeline._summarize(conn, llm, Config(), None, 1, "Three new apps built by Googlers",
                                             "tech", "Support our coverage.", md))
    assert result == "summarized"  # 重要度が低くても要約する
    row = conn.execute("SELECT title_ja, summary FROM items").fetchone()
    assert tuple(row) == ("訳:Three new ", "日本語の要約")
    p = notify.Pending(1, 1, "A", "tech", None, "Three new apps", None, None, "x", None, None, {}, "新しいアプリ")
    assert p.article().title == "新しいアプリ（Three new apps）"


def test_one_word_title_is_not_translated(tmp_path):
    conn = db.connect(":memory:")
    md = item(conn, tmp_path, "zed-industries/zed", "A fast editor.")
    llm = FakeLLM()
    asyncio.run(pipeline._summarize(conn, llm, Config(), None, 1, "zed-industries/zed", "tech", "A fast editor.", md))
    assert llm.translated == [] and conn.execute("SELECT title_ja FROM items").fetchone()[0] is None


def test_translate_post(tmp_path):
    conn = db.connect(":memory:")
    md = item(conn, tmp_path, "Hello world news", "First paragraph.\n\n![img](http://x/i.png) Second [link](http://x).")
    posted = []

    class Client:
        def request(self, method, path, json=None):
            posted.append((method, path, json["content"]))

    from kawarabanya.core.discord_api import DiscordChat

    chats = [DiscordChat(Client())]
    llm = FakeLLM()
    msg = asyncio.run(notify.translate_post(conn, chats, llm, 1))
    assert "スレッドに 1 通" in msg
    assert "http://x" not in llm.translated[0]  # 画像とリンク先は渡さない
    assert md.with_suffix(".ja.md").read_text(encoding="utf-8").startswith("# Hello world news")
    assert posted[0][:2] == ("POST", "/channels/55/messages")
    md.write_text("---\n---\n# 日本語\n\nこれは日本語の記事です。\n", encoding="utf-8")
    with pytest.raises(ValueError):
        asyncio.run(notify.translate_post(conn, chats, llm, 1))
