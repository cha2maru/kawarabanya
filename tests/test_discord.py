import json

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
            return httpx.Response(200, json=ch)
        if path.endswith("/webhooks"):
            cid = path.split("/")[2]
            if req.method == "GET":
                return httpx.Response(200, json=self.webhooks.get(cid, []))
            hook = {"id": f"w{cid}", "name": body["name"], "token": f"k{cid}"}
            self.webhooks.setdefault(cid, []).append(hook)
            return httpx.Response(200, json=hook)
        return httpx.Response(404, json={"message": "not found"})


def setup():
    fake = FakeDiscord()
    client = DiscordClient("TOKEN", transport=httpx.MockTransport(fake.handler))
    conn = db.connect(":memory:")
    for title, cat in [("既存フィード", "tech"), ("新フィード", "tech"), ("ニュース", "news"), ("ホビー誌", "ホビー")]:
        db.insert_feed(conn, f"https://{len(title)}{cat}.example/{title}", title, None, cat)
    return fake, client, conn


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
        a = db.insert_item(conn, feed_id, "a", "https://a", "記事1", "2026-11-07T01:02:03+00:00", "saved")
        db.update_item(conn, a, md_path=str(md))
        b = db.insert_item(conn, feed_id, "b", "https://b", "記事2", "2026-11-07T02:00:00+00:00", "saved")
        db.update_item(conn, b, summary="LLM の要約 @everyone")
        db.insert_item(conn, feed_id, "c", "https://c", "既読", None, "seen")

    stats = notify.notify_pending(
        conn, limit=10, transport=httpx.MockTransport(fake.handler), interval=0
    )
    assert (stats.sent, stats.failed) == (2, 0)
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
    assert rows == {"a": "notified", "b": "notified", "c": "seen"}
    assert notify.notify_pending(conn, limit=10, transport=httpx.MockTransport(fake.handler)).sent == 0


def test_notify_limit_and_missing_channel():
    _, _, conn = setup()
    feed_id = db.list_feeds(conn)[0].id
    with conn:
        db.insert_item(conn, feed_id, "x", "https://x", "x", None, "saved")
    stats = notify.notify_pending(conn, limit=10)
    assert stats.no_channel == 1 and stats.sent == 0
