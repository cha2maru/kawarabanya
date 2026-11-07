import asyncio

import pytest

from kawarabanya.core import db
from kawarabanya.rss import notify, pipeline
from kawarabanya.rss.genres import Genre


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))


def setup():
    conn = db.connect(":memory:")
    fid = db.insert_feed(conn, "https://a.example/rss", "A", None, "tech")
    conn.execute("INSERT INTO items (id, feed_id, key, title, fetched_at, status, discord_thread_id, subgenre, likes)"
                 " VALUES (1, ?, 'k', '記事', '2026-11-01T00:00:00+00:00', 'notified', '100', 'tech/ai', 3)", (fid,))
    for cat, tags in (("tech", '{"sub:tech/net": "9"}'), ("culture", '{"sub:culture/movie": "8"}')):
        conn.execute("INSERT INTO discord_channels (category, guild_id, channel_id, channel_name, webhook_url, tags, created_at)"
                     " VALUES (?, 'g', ?, ?, ?, ?, 'x')", (cat, cat, cat, f"https://hook/{cat}", tags))
    return conn


def test_move_within_forum_changes_tag():
    conn, client = setup(), FakeClient()
    notify.move_post(conn, client, 1, "tech/net")
    assert client.calls == [("PATCH", "/channels/100", {"applied_tags": ["9"]})]
    assert conn.execute("SELECT subgenre FROM items").fetchone()[0] == "tech/net"


def test_move_to_other_forum_posts_then_deletes(monkeypatch):
    conn, client = setup(), FakeClient()
    sent = []
    monkeypatch.setattr(notify, "send_webhook", lambda url, payload: sent.append((url, payload)) or {"channel_id": "200"})
    notify.move_post(conn, client, 1, "culture/movie")
    assert sent[0][0] == "https://hook/culture" and sent[0][1]["applied_tags"] == ["8"]
    assert client.calls == [("DELETE", "/channels/100", None)]
    r = conn.execute("SELECT subgenre, discord_thread_id, likes FROM items").fetchone()
    assert tuple(r) == ("culture/movie", "200", 0)


def test_move_keeps_post_when_repost_fails(monkeypatch):
    conn, client = setup(), FakeClient()

    def fail(url, payload):
        raise notify.DiscordError("HTTP 500")

    monkeypatch.setattr(notify, "send_webhook", fail)
    with pytest.raises(notify.DiscordError):
        notify.move_post(conn, client, 1, "culture/movie")
    assert client.calls == []  # 投稿できなければ、元の投稿を消さない
    assert conn.execute("SELECT discord_thread_id FROM items").fetchone()[0] == "100"


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


def classify(summarizer, title="Ｃｌｏｕｄｆｌａｒｅで障害", **kw):
    conn = db.connect(":memory:")
    fid = db.insert_feed(conn, "https://a.example/rss", "A", None, "news")
    conn.execute("INSERT INTO items (id, feed_id, key, fetched_at, status) VALUES (1, ?, 'k', 'x', 'saved')", (fid,))
    return asyncio.run(pipeline.classify_item(conn, summarizer, GENRES, 1, title, "news", "本文", **kw))


def test_keyword_narrows_candidates():
    s = Chooser((0, 0.6))  # 別のジャンルでも、語があれば MOVE_P より低い確率で採用する
    assert classify(s) == "tech/net"
    assert s.seen == [["テック / ネット"]]


def test_keyword_falls_back_to_all_subgenres():
    s = Chooser((None, 0.9), (1, 0.9))
    assert classify(s) == "culture/movie"  # 語のジャンルと違う記事
    assert len(s.seen[1]) == 3


def test_keyword_genre_when_nothing_matches():
    assert classify(Chooser((None, 0.9), (None, 0.9))) == "tech"


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
