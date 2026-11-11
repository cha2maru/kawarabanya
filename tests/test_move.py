import asyncio

import pytest

from kawarabanya.core import chat, db, discord_api
from kawarabanya.rss import notify, pipeline
from kawarabanya.rss.genres import Genre


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))


def setup(monkeypatch=None, send=None):
    conn = db.connect(":memory:")
    fid = db.insert_feed(conn, "https://a.example/rss", "A", None, "tech")
    conn.execute("INSERT INTO items (id, feed_id, key, title, fetched_at, status, subgenre, likes)"
                 " VALUES (1, ?, 'k', '記事', '2026-11-01T00:00:00+00:00', 'notified', 'tech/ai', 3)", (fid,))
    conn.execute("INSERT INTO posts (item_id, platform, ref, likes, created_at) VALUES (1, 'discord', '100', 3, 'x')")
    for cat, tags in (("tech", {"sub:tech/net": "9"}), ("culture", {"sub:culture/movie": "8"})):
        chat.save_channel(conn, chat.Channel("discord", cat, cat, cat, f"https://hook/{cat}", "g", tags))
    if monkeypatch:
        monkeypatch.setattr(discord_api, "send_webhook", lambda url, payload, transport=None: send(url, payload))
    client = FakeClient()
    return conn, client, [discord_api.DiscordChat(client)]


def test_move_within_forum_changes_tag():
    conn, client, chats = setup()
    notify.move_post(conn, chats, 1, "tech/net")
    assert client.calls == [("PATCH", "/channels/100", {"applied_tags": ["9"]})]
    assert conn.execute("SELECT subgenre FROM items").fetchone()[0] == "tech/net"


def test_move_to_other_forum_posts_then_deletes(monkeypatch):
    sent = []
    conn, client, chats = setup(monkeypatch, lambda url, payload: sent.append((url, payload)) or {"channel_id": "200"})
    notify.move_post(conn, chats, 1, "culture/movie")
    assert sent[0][0] == "https://hook/culture" and "applied_tags" not in sent[0][1]
    assert client.calls == [("DELETE", "/channels/100", None)]
    assert tuple(conn.execute("SELECT subgenre, likes FROM items").fetchone()) == ("culture", 0)  # 別のジャンルへはサブジャンルなし
    assert tuple(conn.execute("SELECT ref, url FROM posts").fetchone()) == ("200", "https://discord.com/channels/g/200")


def test_move_keeps_post_when_repost_fails(monkeypatch):
    def fail(url, payload):
        raise discord_api.DiscordError("HTTP 500")

    conn, client, chats = setup(monkeypatch, fail)
    with pytest.raises(discord_api.DiscordError):
        notify.move_post(conn, chats, 1, "culture/movie")
    assert client.calls == []  # 投稿できなければ、元の投稿を消さない
    assert conn.execute("SELECT ref FROM posts").fetchone()[0] == "100"


class Chooser:
    """候補の名前を受け取り、answers の順に答える。"""
    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    async def classify(self, title, md, options):
        self.seen.append([name for name, _ in options])
        return self.answers.pop(0)


GENRES = {
    "news": Genre("news", "ニュース"),
    "tech": Genre("tech", "テック", keywords=["Cloudflare"]),
    "tech/net": Genre("tech/net", "ネット"),
    "culture": Genre("culture", "文化"),
    "culture/movie": Genre("culture/movie", "映画"),
    "news/world": Genre("news/world", "国際"),
}


def classify(summarizer, title="Ｃｌｏｕｄｆｌａｒｅで障害", body="本文", **kw):
    conn = db.connect(":memory:")
    fid = db.insert_feed(conn, "https://a.example/rss", "A", None, "news")
    conn.execute("INSERT INTO items (id, feed_id, key, fetched_at, status) VALUES (1, ?, 'k', 'x', 'saved')", (fid,))
    return asyncio.run(pipeline.classify_item(conn, summarizer, GENRES, 1, title, "news", body, **kw))


def test_keyword_narrows_candidates():
    s = Chooser((0, 0.9))
    assert classify(s) == "tech/net"
    assert s.seen == [["テック / ネット"]]


def test_keyword_fixes_genre_when_subgenre_is_unsure():
    assert classify(Chooser((0, 0.5))) == "tech"  # 合うサブジャンルがなくても、ジャンルは語のもの
    assert classify(Chooser((None, 0.9))) == "tech"


def test_keyword_in_lead():
    s = Chooser((None, 0.9))
    assert classify(s, title="便利なバッグ", body="こちらはＣｌｏｕｄｆｌａｒｅが書いた記事です。") == "tech"
    assert classify(Chooser((None, 0.9)), title="便利なバッグ") == "news"


def test_keyword_ignored_for_posted_items():
    s = Chooser((None, 0.9))
    assert classify(s, allow_move=False) == "news"
    assert s.seen == [["ニュース / 国際"]]


def test_merge_keeps_keywords(tmp_path):
    from kawarabanya.rss.genres import load_genres
    from kawarabanya.rss.suggest import Merge, apply

    p = tmp_path / "genres.toml"
    p.write_text('[tech]\nlabel = "テック"\n[tech.a]\nlabel = "A"\nkeywords = ["甲"]\n[tech.b]\nlabel = "B"\nkeywords = ["乙"]\n',
                 encoding="utf-8")
    apply(db.connect(":memory:"), p, [], [Merge("tech", ["a", "b"], "a", "合", "説明")])
    defs = load_genres(p)
    assert defs["tech/a"].keywords == ["甲", "乙"] and "tech/b" not in defs


def test_suggest_verifies_moved_items_within_new_genre():
    from kawarabanya.rss.suggest import Suggestion, verify

    defs = dict(GENRES)
    moved = {"id": 1, "title": "t", "md_path": None, "category": "tech", "subgenre": "culture"}
    stayed = {"id": 2, "title": "t", "md_path": None, "category": "culture", "subgenre": "culture"}
    s = Chooser((1, 0.9), (0, 0.9))
    suggestion = Suggestion("culture", "shop", "買い物", "", [1, 2])
    asyncio.run(verify(s, defs, suggestion, {1: moved, 2: stayed}))
    assert len(s.seen[0]) < len(s.seen[1])  # 移した記事の候補は culture の中だけ
