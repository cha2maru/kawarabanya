import json
from datetime import UTC, datetime, timedelta

import httpx

from feedpipe import db, discord_api, notify
from feedpipe.discord_api import (
    GUILD_FORUM,
    DiscordClient,
    apply_plan,
    channel_name,
    make_plan,
)


class FakeDiscord:
    """フォーラム・タグ・Webhook の作成と、Webhook への投稿を真似る。"""

    def __init__(self):
        self.channels = [
            {"id": "10", "type": 4, "name": "feedpipe"},
            {"id": "11", "type": GUILD_FORUM, "name": "tech", "parent_id": "10",
             "available_tags": [{"id": "t1", "name": "既存フィード"}]},
            {"id": "12", "type": GUILD_FORUM, "name": "news"},  # 親カテゴリが違うので別物
        ]
        self.webhooks: dict[str, list] = {"11": [{"id": "w11", "name": "feedpipe", "token": "k11"}]}
        self.posts: list[dict] = []
        self.likes: dict[str, int] = {}
        self.next_id = 100
        self.rate_limited_once = False

    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v10")
        body = json.loads(req.content) if req.content else None
        if req.url.path.startswith("/api/webhooks/"):  # Webhook URL はバージョンなし・認証なし
            if not self.rate_limited_once:
                self.rate_limited_once = True
                return httpx.Response(429, json={"retry_after": 0.01})
            self.posts.append(body)
            return httpx.Response(200, json={"id": self._id(), "channel_id": f"thread{len(self.posts)}"})
        assert req.headers["authorization"] == "Bot TOKEN"
        if path == "/users/@me/guilds":
            return httpx.Response(200, json=[{"id": "1", "name": "srv"}])
        if path == "/guilds/1/channels" and req.method == "GET":
            return httpx.Response(200, json=self.channels)
        if path == "/guilds/1/channels" and req.method == "POST":
            tags = [{"id": self._id(), **t} for t in body.pop("available_tags", [])]
            ch = {"id": self._id(), **body, "available_tags": tags}
            self.channels.append(ch)
            return httpx.Response(201, json=ch)
        if req.method == "PATCH" and path.startswith("/channels/"):
            ch = next(c for c in self.channels if c["id"] == path.split("/")[2])
            if "available_tags" in body:
                ch["available_tags"] = [t if "id" in t else {"id": self._id(), **t} for t in body["available_tags"]]
            if "nsfw" in body:
                ch["nsfw"] = body["nsfw"]
            if "default_reaction_emoji" in body:
                ch["default_reaction_emoji"] = body["default_reaction_emoji"]
            return httpx.Response(200, json=ch)
        if "/messages/" in path:  # フォーラム投稿の最初のメッセージ
            tid = path.split("/")[2]
            if tid not in self.likes:
                return httpx.Response(404, json={"message": "Unknown Message"})
            return httpx.Response(200, json={"id": tid, "reactions": [
                {"emoji": {"id": None, "name": "👍"}, "count": self.likes[tid]},
                {"emoji": {"id": None, "name": "🎉"}, "count": 9},
            ]})
        if path.endswith("/webhooks"):
            cid = path.split("/")[2]
            if req.method == "GET":
                return httpx.Response(200, json=self.webhooks.get(cid, []))
            hook = {"id": f"w{cid}", "name": body["name"], "token": f"k{cid}"}
            self.webhooks.setdefault(cid, []).append(hook)
            return httpx.Response(200, json=hook)
        return httpx.Response(404, json={"message": "not found"})


def ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat(timespec="seconds")


def setup():
    fake = FakeDiscord()
    client = DiscordClient("TOKEN", transport=httpx.MockTransport(fake.handler))
    conn = db.connect(":memory:")
    for title, cat in [("既存フィード", "tech"), ("新フィード", "tech"), ("ニュース", "news"), ("ホビー誌", "ホビー")]:
        db.insert_feed(conn, f"https://{len(title)}{cat}.example/{title}", title, None, cat)
    return fake, client, conn


def test_tag_name_strips_after_truncation():
    feed = db.Feed(id=1, url="u", original_url="u", title="はてなブックマーク - キーワード - コミティア", site_url=None,
                   category=None, enabled=True, disabled_reason=None, last_status=None, last_error=None,
                   last_success_at=None, latest_entry_at=None, fail_count=0, etag=None, last_modified=None,
                   initialized_at=None)
    assert discord_api.tag_name(feed) == "はてなブックマーク - キーワード -"


def test_channel_name():
    assert channel_name("tech") == "tech"
    assert channel_name("News Daily/Sub") == "news-daily-sub"
    assert channel_name("注目度1-100") == "注目度1-100"
    assert channel_name("LifeHackパック") == "lifehackパック"
    assert channel_name(None) == "uncategorized"


def test_plan_and_apply_forums():
    fake, client, conn = setup()
    guild = discord_api.resolve_guild(client, None)
    plan = make_plan(client, guild, discord_api.feeds_by_category(conn), "feedpipe")
    assert plan.parent_id == "10"
    assert [(p.name, p.exists) for p in plan.channels] == [("news", False), ("tech", True), ("ホビー", False)]
    tech = plan.channels[1]
    assert tech.missing_tags() == ["新フィード"]
    assert tech.extra_tags() == []

    created = apply_plan(client, conn, plan, "feedpipe", nsfw=True)
    assert [p.name for p in created] == ["news", "ホビー"]
    hobby = next(c for c in fake.channels if c["name"] == "ホビー")
    assert hobby["type"] == GUILD_FORUM and hobby["parent_id"] == "10"
    assert hobby["nsfw"] is True
    assert [t["name"] for t in hobby["available_tags"]] == ["ホビー誌"]
    tech_ch = next(c for c in fake.channels if c["id"] == "11")
    assert [t["name"] for t in tech_ch["available_tags"]] == ["既存フィード", "新フィード"]
    assert tech_ch["available_tags"][0]["id"] == "t1"  # 既存のタグを消さない
    assert tech_ch["nsfw"] is True  # 既存のフォーラムも年齢制限にそろえる

    rows = {r["category"]: r for r in conn.execute("SELECT * FROM discord_channels")}
    assert rows["tech"]["webhook_url"] == "https://discord.com/api/webhooks/w11/k11"  # 既存を再利用
    assert len(json.loads(rows["tech"]["tags"])) == 2

    # もう一度実行しても作り直さない
    again = make_plan(client, guild, discord_api.feeds_by_category(conn), "feedpipe")
    assert all(p.exists and not p.missing_tags() and p.nsfw for p in again.channels)
    assert apply_plan(client, conn, again, "feedpipe", nsfw=True) == []


def test_notify_posts_forum_threads(tmp_path):
    fake, client, conn = setup()
    plan = make_plan(client, {"id": "1", "name": "srv"}, discord_api.feeds_by_category(conn), "feedpipe")
    apply_plan(client, conn, plan, "feedpipe")

    md = tmp_path / "a.md"
    md.write_text('---\ntitle: "x"\n---\n\n# 記事1\n\n本文の **先頭** です。[リンク](https://l) 20\\~50% a\\_b\n\n---\n\n元記事: <https://a>\n')
    feed_id = db.find_feed_by_url(conn, "https://6tech.example/既存フィード").id
    with conn:
        a = db.insert_item(conn, feed_id, "a", "https://a", "記事1", ago(hours=5), "saved")
        db.update_item(conn, a, md_path=str(md))
        b = db.insert_item(conn, feed_id, "b", "https://b", "記事2", ago(hours=1), "saved")
        db.update_item(conn, b, summary="LLM の要約 @everyone")
        db.insert_item(conn, feed_id, "c", "https://c", "既読", None, "seen")
        db.insert_item(conn, feed_id, "d", "https://d", "古い記事", ago(days=4), "saved")

    stats = notify.notify_pending(
        conn, limit=10, max_age_days=3, transport=httpx.MockTransport(fake.handler), interval=0
    )
    assert (stats.sent, stats.failed, stats.stale) == (2, 0, 1)
    p1, p2 = fake.posts
    assert p1["thread_name"] == "記事1"
    assert p1["username"] == "既存フィード"
    assert p1["applied_tags"] == ["t1"]
    assert p1["content"].startswith("本文の 先頭 です。リンク 20~50% a_b")
    assert "🕒 " in p1["content"]
    assert p1["content"].endswith("https://a")
    assert p2["content"].startswith("LLM の要約")
    assert p2["allowed_mentions"] == {"parse": []}

    rows = dict(conn.execute("SELECT key, status FROM items").fetchall())
    assert rows == {"a": "notified", "b": "notified", "c": "seen", "d": "stale"}
    again = notify.notify_pending(conn, limit=10, max_age_days=3, transport=httpx.MockTransport(fake.handler))
    assert (again.sent, again.stale) == (0, 0)


def test_notify_limit_and_missing_channel():
    _, _, conn = setup()
    feed_id = db.list_feeds(conn)[0].id
    with conn:
        db.insert_item(conn, feed_id, "x", "https://x", "x", None, "saved")
    stats = notify.notify_pending(conn, limit=10, max_age_days=3)
    assert stats.no_channel == 1 and stats.sent == 0


def test_subgenre_tags(tmp_path):
    from feedpipe.genres import load_genres

    fake = FakeDiscord()
    fake.channels = []
    client = DiscordClient("TOKEN", transport=httpx.MockTransport(fake.handler))
    conn = db.connect(":memory:")
    fid = db.insert_feed(conn, "https://w.example/rss", "世界ニュース", None, "news/world")
    db.insert_feed(conn, "https://d.example/rss", "国内ニュース", None, "news")
    g = tmp_path / "genres.toml"
    g.write_text('[news]\ndescription = "総合"\n[news.world]\nlabel = "国際"\n', encoding="utf-8")

    plan = make_plan(client, {"id": "1", "name": "srv"}, discord_api.feeds_by_category(conn), "feedpipe",
                     load_genres(g))
    [pc] = plan.channels  # news/world も news のフォーラムにまとまる
    assert pc.name == "news" and pc.wanted_tags() == ["国際", "国内ニュース", "世界ニュース"]
    apply_plan(client, conn, plan, "feedpipe")
    forum = next(c for c in fake.channels if c["type"] == GUILD_FORUM)
    assert forum["topic"] == "総合"

    with conn:
        db.insert_item(conn, fid, "k", "https://w.example/1", "記事", ago(hours=1), "saved")
    notify.notify_pending(conn, limit=5, max_age_days=3, transport=httpx.MockTransport(fake.handler), interval=0)
    tag_by_name = {t["name"]: t["id"] for t in forum["available_tags"]}
    assert fake.posts[-1]["applied_tags"] == [tag_by_name["国際"], tag_by_name["世界ニュース"]]


def test_removes_unused_tags():
    fake, client, conn = setup()
    tech = next(c for c in fake.channels if c["id"] == "11")
    tech["available_tags"].append({"id": "old", "name": "消えたフィード"})
    plan = make_plan(client, {"id": "1", "name": "srv"}, discord_api.feeds_by_category(conn, ["tech"]), "feedpipe")
    assert [t["name"] for t in plan.channels[0].extra_tags()] == ["消えたフィード"]
    apply_plan(client, conn, plan, "feedpipe")
    assert [t["name"] for t in tech["available_tags"]] == ["既存フィード", "新フィード"]
    assert tech["available_tags"][0]["id"] == "t1"  # 残すタグは id を保つ


def test_default_reaction_and_like_sync():
    fake, client, conn = setup()
    plan = make_plan(client, {"id": "1", "name": "srv"}, discord_api.feeds_by_category(conn), "feedpipe")
    apply_plan(client, conn, plan, "feedpipe")
    forums = [c for c in fake.channels if c["type"] == GUILD_FORUM and c.get("parent_id") == "10"]
    assert all(c["default_reaction_emoji"]["emoji_name"] == "👍" for c in forums)  # 新規も既存も

    feed_id = db.list_feeds(conn)[0].id
    with conn:
        ids = {}
        for key, tid in [("a", "t-a"), ("b", "t-b"), ("gone", "t-gone")]:
            ids[key] = db.insert_item(conn, feed_id, key, None, key, ago(hours=1), "notified")
            db.update_item(conn, ids[key], discord_thread_id=tid, notified_at=ago(hours=1))
    fake.likes = {"t-a": 2, "t-b": 0}

    assert notify.sync_likes(conn, client, interval_hours=6) == 3
    likes = dict(conn.execute("SELECT key, likes FROM items WHERE key IN ('a','b','gone')").fetchall())
    assert likes == {"a": 2, "b": 0, "gone": None}
    assert notify.sync_likes(conn, client, interval_hours=6) == 0  # 6 時間以内は再確認しない
    assert notify.sync_likes(conn, client, interval_hours=0) == 3
