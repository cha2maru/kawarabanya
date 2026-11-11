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
    assert sched["is_private"] and ("conversations.invite", {"channel": sched["id"], "users": "U42"}) in fake.calls
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
