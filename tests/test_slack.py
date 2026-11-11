from urllib.parse import parse_qs

import httpx
import pytest

from kawarabanya.core import chat, db, slack_api
from kawarabanya.core.chat import DIGEST_KEY, SCHEDULE_KEY, Article
from kawarabanya.core.config import Config
from kawarabanya.rss import notify


class FakeSlack:
    """Slack の Web API の代わり（フォーム形式で受け、ok 付きの JSON を返す）。"""

    def __init__(self):
        self.channels = [{"id": "C1", "name": "kawarabanya-tech", "is_member": False}]
        self.messages: dict[str, dict] = {}  # "チャンネル/ts" → メッセージ
        self.calls: list[tuple[str, dict]] = []
        self.ts = 1700000000
        self.limited_once = False

    def handler(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer xoxb-T"
        method = req.url.path.removeprefix("/api/")
        p = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        self.calls.append((method, p))
        ok = lambda **kw: httpx.Response(200, json={"ok": True, **kw})  # noqa: E731
        if method == "chat.postMessage" and not self.limited_once:
            self.limited_once = True
            return httpx.Response(429, headers={"Retry-After": "0"})
        if method == "auth.test":
            return ok(user_id="UBOT", user="kawarabanya", team="ws", team_id="T1", url="https://ws.slack.com/")
        if method == "conversations.list":
            return ok(channels=self.channels, response_metadata={"next_cursor": ""})
        if method == "conversations.create":
            ch = {"id": f"C{len(self.channels) + 1}", "name": p["name"], "is_member": True,
                  "is_private": p["is_private"] == "true"}
            self.channels.append(ch)
            return ok(channel=ch)
        if method == "conversations.invite" and getattr(self, "invited_already", False):
            return httpx.Response(200, json={"ok": False, "error": "already_in_channel"})
        if method in ("conversations.join", "conversations.invite", "reactions.add"):
            return ok()
        if method == "chat.postMessage":
            self.ts += 1
            ref = f"{p['channel']}/{self.ts}.000100"
            self.messages[ref] = {**p, "reactions": [{"name": "+1", "count": 1, "users": ["UBOT"]},
                                                     {"name": "-1", "count": 1, "users": ["UBOT"]}]}
            return ok(channel=p["channel"], ts=f"{self.ts}.000100")
        ref = f"{p.get('channel')}/{p.get('ts') or p.get('timestamp')}"
        if ref not in self.messages:
            return httpx.Response(200, json={"ok": False, "error": "message_not_found"})
        if method == "chat.update":
            self.messages[ref]["text"] = p["text"]
            return ok()
        if method == "chat.delete":
            del self.messages[ref]
            return ok()
        if method == "reactions.get":
            return ok(message={"reactions": self.messages[ref]["reactions"]})
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})


def setup():
    fake = FakeSlack()
    client = slack_api.SlackClient("xoxb-T", transport=httpx.MockTransport(fake.handler))
    return fake, client, slack_api.SlackChat(client, mention_user_id="U42")


def test_markdown_to_slack():
    text = "## 📰 テック のまとめ\n**注目の記事**\n- [A & B ［速報］](https://x/?a=1&b=2)  👍2"
    assert slack_api.from_markdown(text) == (
        "*📰 テック のまとめ*\n*注目の記事*\n- <https://x/?a=1&amp;b=2|A &amp; B ［速報］>  👍2")
    assert slack_api.channel_name("kawarabanya-", "news/world") == "kawarabanya-news-world"


def test_post_article_and_reactions():
    fake, _client, sc = setup()
    ch = chat.Channel("slack", "tech", "C1", "kawarabanya-tech", None, "T1", {"sub:tech/ai": "AI"})
    a = Article("<新>モデル", "ITmedia", "要約です", "https://a", "2026-11-11T00:00:00+00:00", "tech/ai")
    posted = sc.post_article(ch, a)
    assert posted.ref == "C1/1700000001.000100"
    assert posted.url == "https://ws.slack.com/archives/C1/p1700000001000100"
    msg = fake.messages[posted.ref]
    assert msg["text"].startswith("[AI] *&lt;新&gt;モデル*\n要約です") and msg["text"].endswith("https://a")
    assert msg["username"] == "ITmedia" and msg["unfurl_links"] == "false"
    assert [c[1]["name"] for c in fake.calls if c[0] == "reactions.add"] == ["+1", "-1"]  # 押しやすいように先に付ける
    assert sc.reactions(posted.ref) == (0, 0)  # Bot が付けた分は数えない
    fake.messages[posted.ref]["reactions"][0] = {"name": "+1", "count": 3, "users": ["UBOT", "U42", "U9"]}
    assert sc.reactions(posted.ref) == (2, 0)

    sc.retag(ch, posted.ref, Article("<新>モデル", "ITmedia", "要約です", "https://a", None, "tech"))
    assert fake.messages[posted.ref]["text"].startswith("*&lt;新&gt;モデル*")  # ラベルなし
    assert sc.reply(posted.ref, "訳") == 1
    assert fake.calls[-1][1]["thread_ts"] == "1700000001.000100"
    sc.delete_post(posted.ref)
    sc.delete_post(posted.ref)  # すでに消えていても失敗にしない
    assert sc.reactions(posted.ref) is None
    with pytest.raises(chat.PostGoneError):
        sc.retag(ch, posted.ref, a)


def test_send_digest_with_mention():
    fake, _client, sc = setup()
    ch = chat.Channel("slack", DIGEST_KEY, "C9", "kawarabanya-digest")
    ref = sc.send(ch, "**話題**\n- [x](https://x)", username="瓦版屋 まとめ", mention=True)
    assert fake.messages[ref]["text"] == "<@U42> *話題*\n- <https://x|x>"
    sc.edit(ch, ref, "## 更新")
    assert fake.messages[ref]["text"] == "*更新*"
    sc.remove(ch, ref)
    assert ref not in fake.messages


def test_slack_setup_and_notify(tmp_path, monkeypatch):
    from kawarabanya.cli import slack as cli_slack

    fake, client, sc = setup()
    monkeypatch.setattr(cli_slack, "_client", lambda cfg: client)
    conn = db.connect(":memory:")
    fid = db.insert_feed(conn, "https://a.example/rss", "A", None, "tech")
    g = tmp_path / "genres.toml"
    g.write_text('[tech]\n[tech.ai]\nlabel = "AI"\n', encoding="utf-8")
    from kawarabanya.rss.genres import load_genres

    cfg = Config()
    cfg.slack.mention_user_id = "U42"
    cfg.schedule.enabled = True
    lines = cli_slack.setup_channels(cfg, conn, load_genres(g), apply=True)
    assert [ln.split()[0] for ln in lines] == ["既存", "作成", "作成"]
    assert ("conversations.join", {"channel": "C1"}) in fake.calls  # 既存の公開チャンネルに入る
    sched = next(c for c in fake.channels if c["name"] == "kawarabanya-schedule")
    assert sched["is_private"]
    invited = {p["channel"] for m, p in fake.calls if m == "conversations.invite" and p["users"] == "U42"}
    assert invited == {"C1", sched["id"], next(c["id"] for c in fake.channels if c["name"] == "kawarabanya-digest")}
    fake.invited_already = True
    cli_slack.setup_channels(cfg, conn, load_genres(g), apply=True)  # 入っていれば already_in_channel でも続ける
    assert chat.channel(conn, "slack", "tech").tags == {"sub:tech/ai": "AI"}
    assert chat.channel(conn, "slack", SCHEDULE_KEY).channel_id == sched["id"]

    with conn:
        iid = db.insert_item(conn, fid, "k", "https://a/1", "記事", None, "saved")
        db.update_item(conn, iid, subgenre="tech/ai", summary="要約")
    stats = notify.notify_pending(conn, [sc], limit=5, max_age_days=3, interval=0)
    assert stats.sent == 1
    ref, url = conn.execute("SELECT ref, url FROM posts WHERE platform = 'slack'").fetchone()
    assert fake.messages[ref]["text"].startswith("[AI] *記事*")
    assert url.startswith("https://ws.slack.com/archives/C1/p")
    assert notify.sync_likes(conn, [sc], interval_hours=0) == 1
    assert chat.count_reaction(conn, "slack", ref, chat.LIKE, 1)
    assert conn.execute("SELECT likes FROM items").fetchone()[0] == 1


class FakeWeb:
    """slack_sdk の AsyncWebClient の代わり。"""

    def __init__(self):
        self.calls = []

    async def chat_postMessage(self, **kw):  # noqa: N802 slack_sdk の名前に合わせる
        self.calls.append(("post", kw))
        return {"channel": kw["channel"], "ts": "9.9"}

    async def chat_update(self, **kw):
        self.calls.append(("update", kw))
        return {}


class FakeBot:
    def __init__(self, db_path, intent):
        self.db_path, self.intent, self.ran, self.asked = db_path, intent, [], []

    async def make_intent(self, text, platform, ref):
        self.asked.append((text, platform, ref))
        return self.intent

    async def run_command(self, intent):
        self.ran.append(intent.argv)
        return f"✅ {intent.summary}\n```\nok\n```"


def test_slack_bridge(tmp_path):
    import asyncio

    from kawarabanya.bot import assistant
    from kawarabanya.bot.slack_bot import SlackBridge

    path = tmp_path / "k.db"
    conn = db.connect(path)
    fid = db.insert_feed(conn, "https://a.example/rss", "A", None, "tech")
    with conn:
        iid = db.insert_item(conn, fid, "k", None, "記事", None, "notified")
        conn.execute("INSERT INTO posts (item_id, platform, ref, created_at) VALUES (?, 'slack', 'C1/1.1', 'x')", (iid,))
    intent = assistant.Intent("feed_remove", ["feeds", "remove", "1"], "A を消す", confirm=True)
    bot, web = FakeBot(path, intent), FakeWeb()
    bridge = SlackBridge(bot, web, "UBOT", "U42")
    run = asyncio.run

    # リアクション: Bot が先に付けた分は数えない。肌の色の違いも 👍
    for user, reaction in [("UBOT", "+1"), ("U42", "+1::skin-tone-2"), ("U42", "-1"), ("U42", "tada")]:
        run(bridge.handle("events_api", {"event": {"type": "reaction_added", "user": user, "reaction": reaction,
                                                   "item": {"type": "message", "channel": "C1", "ts": "1.1"}}}))
    assert tuple(conn.execute("SELECT likes, dislikes FROM items").fetchone()) == (1, 1)

    # 持ち主でなければ断る
    mention = {"type": "app_mention", "user": "U9", "text": "<@UBOT> A を消して", "channel": "C1", "ts": "2.2",
               "thread_ts": "1.1"}
    run(bridge.handle("events_api", {"event": mention}))
    assert "持ち主" in web.calls[-1][1]["text"] and not bot.asked

    # スレッドの中の依頼は、元の投稿（記事）についての依頼。変更するものはボタンで確かめる
    run(bridge.handle("events_api", {"event": {**mention, "user": "U42"}}))
    assert bot.asked == [("A を消して", "slack", "C1/1.1")]
    kind, kw = web.calls[-1]
    assert kind == "post" and kw["thread_ts"] == "1.1" and kw["blocks"][1]["elements"][0]["action_id"] == "run"
    key = kw["blocks"][1]["elements"][0]["value"]
    click = {"type": "block_actions", "user": {"id": "U42"}, "channel": {"id": "C1"}, "message": {"ts": "9.9"},
             "actions": [{"action_id": "run", "value": key}]}
    run(bridge.handle("interactive", {**click, "user": {"id": "U9"}}))  # 持ち主でなければ何もしない
    assert bot.ran == []
    run(bridge.handle("interactive", click))
    assert bot.ran == [["feeds", "remove", "1"]] and web.calls[-1][1]["text"].startswith("✅ A を消す")
    run(bridge.handle("interactive", click))  # 2 回目は実行しない
    assert bot.ran == [["feeds", "remove", "1"]] and "実行済み" in web.calls[-1][1]["text"]
