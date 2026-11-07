import asyncio
from types import SimpleNamespace

from kawarabanya.bot import client as botmod
from kawarabanya.core.config import Config, GeneralConfig


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Attachment:
    def __init__(self, filename: str, data: bytes):
        self.filename, self.size, self.data = filename, len(data), data

    async def save(self, path):
        path.write_bytes(self.data)


def make_bot(tmp_path, answer: dict):
    (tmp_path / "genres.toml").write_text('[tech]\nlabel = "テック"\n', encoding="utf-8")
    cfg = Config(general=GeneralConfig(db=str(tmp_path / "data" / "k.db"), genres_file=str(tmp_path / "genres.toml")))
    bot = botmod.Bot(cfg, [])
    me = SimpleNamespace(id=1, name="bot")
    bot._connection.user = me

    class FakeLLM:
        async def ask_json(self, *args, **kw):
            return {"command": "unknown", "url": "", "genre": "", "feed": "", "level": "", **answer}

    bot.summarizer = FakeLLM()
    ran = []

    async def fake_run(*argv):
        ran.append(argv)
        return 0, "ok"

    bot._kawarabanya = fake_run
    return bot, me, ran


def message(me, author_id, owner_id, content, attachments=()):
    replies = []

    async def reply(text, **kw):
        replies.append((text, kw))
        return SimpleNamespace(edit=lambda **kw: asyncio.sleep(0, replies.append(("edit", kw))))

    guild = SimpleNamespace(owner_id=owner_id, me=SimpleNamespace(roles=[]))
    return SimpleNamespace(
        author=SimpleNamespace(bot=False, id=author_id), guild=guild, mentions=[me], role_mentions=[],
        content=f"<@1> {content}", attachments=list(attachments), id=42,
        channel=SimpleNamespace(id=7, typing=Typing), reply=reply,
    ), replies


def test_only_owner_can_ask(tmp_path):
    bot, me, ran = make_bot(tmp_path, {"command": "status"})
    msg, replies = message(me, author_id=2, owner_id=9, content="いまどうなってる？")
    asyncio.run(bot.on_message(msg))
    assert "持ち主だけ" in replies[0][0] and ran == []


def test_opml_attachment_is_saved_and_confirmed(tmp_path):
    bot, me, ran = make_bot(tmp_path, {"command": "opml_import"})
    msg, replies = message(me, 9, 9, "これ取り込んで", [Attachment("subs.opml", b"<opml/>")])
    asyncio.run(bot.on_message(msg))
    saved = tmp_path / "data" / "uploads" / "42_subs.opml"
    assert saved.read_bytes() == b"<opml/>"
    text, kw = replies[0]
    assert "import-opml" in text and str(saved) in text and kw["view"].intent.confirm
    assert ran == []  # ボタンを押すまで実行しない


def test_readonly_request_runs_without_waiting_for_lock(tmp_path):
    bot, me, ran = make_bot(tmp_path, {"command": "status"})
    msg, replies = message(me, 9, 9, "いまどうなってる？")

    async def go():
        await bot._lock.acquire()  # 定期処理が動いている
        await asyncio.wait_for(bot.on_message(msg), 2)

    asyncio.run(go())
    assert ran == [("status",)] and "✅" in replies[-1][1]["content"]


def test_job_failures_alert_after_three(tmp_path, monkeypatch):
    bot, _me, _ran = make_bot(tmp_path, {})
    sent = []
    monkeypatch.setattr(botmod.health, "send_alerts", lambda conn, found: sent.append(dict(found)))

    async def fail(*argv):
        return 1, "エラー"

    bot._kawarabanya = fail

    async def go(times):
        for _ in range(times):
            await bot.cycle()

    asyncio.run(go(2))
    assert not any("job:run" in s for s in sent)  # 1〜2 回は一時的なことが多い
    asyncio.run(go(1))
    assert "job:run" in sent[-1] and "job:digest" in sent[-1]


def test_wait_llm_until_healthy(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    answers = [503, 200]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(answers.pop(0))
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    bot, _, _ = make_bot(tmp_path, {})
    bot.cfg.summarize.endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    asyncio.run(bot._wait_llm(timeout=5, interval=0))
    server.shutdown()
    assert answers == []  # 503 の後、200 が返るまで待った
