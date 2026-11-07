import json

import httpx

from feedpipe import db, discord_api
from feedpipe.discord_api import DiscordClient, apply_plan, channel_name, make_plan


class FakeDiscord:
    """チャンネルと Webhook の作成だけを真似る。"""

    def __init__(self):
        self.channels = [
            {"id": "10", "type": 4, "name": "feedpipe"},
            {"id": "11", "type": 0, "name": "tech", "parent_id": "10"},
            {"id": "12", "type": 0, "name": "news"},  # 親カテゴリが違うので別物
        ]
        self.webhooks: dict[str, list] = {"11": [{"id": "w11", "name": "feedpipe", "token": "t11"}]}
        self.next_id = 100
        self.rate_limited_once = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bot TOKEN"
        path = req.url.path.removeprefix("/api/v10")
        if path == "/users/@me/guilds":
            return httpx.Response(200, json=[{"id": "1", "name": "srv"}])
        if path == "/guilds/1/channels" and req.method == "GET":
            return httpx.Response(200, json=self.channels)
        if path == "/guilds/1/channels" and req.method == "POST":
            if not self.rate_limited_once:
                self.rate_limited_once = True
                return httpx.Response(429, json={"retry_after": 0.01})
            body = json.loads(req.content)
            ch = {"id": str(self.next_id), **body}
            self.next_id += 1
            self.channels.append(ch)
            return httpx.Response(201, json=ch)
        if path.endswith("/webhooks"):
            cid = path.split("/")[2]
            if req.method == "GET":
                return httpx.Response(200, json=self.webhooks.get(cid, []))
            hook = {"id": f"w{cid}", "name": json.loads(req.content)["name"], "token": f"t{cid}"}
            self.webhooks.setdefault(cid, []).append(hook)
            return httpx.Response(200, json=hook)
        return httpx.Response(404, json={"message": "not found"})


def test_channel_name():
    assert channel_name("tech") == "tech"
    assert channel_name("News Daily/Sub") == "news-daily-sub"
    assert channel_name("注目度1-100") == "注目度1-100"
    assert channel_name("LifeHackパック") == "lifehackパック"
    assert channel_name(None) == "uncategorized"


def test_plan_and_apply():
    fake = FakeDiscord()
    client = DiscordClient("TOKEN", transport=httpx.MockTransport(fake.handler))
    conn = db.connect(":memory:")
    guild = discord_api.resolve_guild(client, None)

    plan = make_plan(client, guild, ["tech", "news", "ホビー"], "feedpipe")
    assert plan.parent_id == "10"
    assert [(p.name, p.exists) for p in plan.channels] == [("tech", True), ("news", False), ("ホビー", False)]

    created = apply_plan(client, conn, plan, "feedpipe")
    assert [p.name for p in created] == ["news", "ホビー"]
    rows = dict(conn.execute("SELECT category, webhook_url FROM discord_channels").fetchall())
    assert rows["tech"] == "https://discord.com/api/webhooks/w11/t11"  # 既存の Webhook を再利用
    assert len(rows) == 3
    new = next(c for c in fake.channels if c["name"] == "ホビー")
    assert new["parent_id"] == "10" and new["topic"] == "feedpipe: ホビー"

    # もう一度実行しても作り直さない
    again = make_plan(client, guild, ["tech", "news", "ホビー"], "feedpipe")
    assert all(p.exists for p in again.channels)
    assert apply_plan(client, conn, again, "feedpipe") == []


def test_creates_parent_category():
    fake = FakeDiscord()
    fake.channels = []
    client = DiscordClient("TOKEN", transport=httpx.MockTransport(fake.handler))
    plan = make_plan(client, {"id": "1", "name": "srv"}, ["tech"], "feedpipe")
    assert plan.parent_id is None
    apply_plan(client, db.connect(":memory:"), plan, "feedpipe")
    parent = next(c for c in fake.channels if c["type"] == 4)
    child = next(c for c in fake.channels if c["type"] == 0)
    assert child["parent_id"] == parent["id"]
