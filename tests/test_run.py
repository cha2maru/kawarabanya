import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from feedpipe import db
from feedpipe.config import Config, GeneralConfig
from feedpipe.rss import archive
from feedpipe.rss.item import Item
from feedpipe.rss.pipeline import run_feeds

ARTICLE = """<html><head><title>{t}</title></head><body><nav>メニュー</nav>
<article><h1>{t}</h1>{body}</article><footer>フッター</footer></body></html>"""


def rss(items: list[tuple[str, str]]) -> bytes:
    xs = "".join(
        f"<item><title>{t}</title><link>https://site.example/{k}</link><guid>{k}</guid>"
        f"<pubDate>Mon, 0{i + 1} Nov 2026 00:00:00 GMT</pubDate>"
        f"<description>要約 {t}</description></item>"
        for i, (k, t) in enumerate(items)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>F</title>{xs}</channel></rss>'.encode()


class Site:
    def __init__(self):
        self.items = [("a", "記事A"), ("b", "記事B"), ("c", "記事C")]
        self.etag = '"v1"'
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if request.url.path == "/feed":
            if request.headers.get("if-none-match") == self.etag:
                return httpx.Response(304)
            return httpx.Response(200, content=rss(self.items), headers={"etag": self.etag})
        key = request.url.path.strip("/")
        if key == "broken":
            return httpx.Response(500)
        title = dict(self.items)[key]
        body = "".join(f"<p>{title} の本文 {n} 段落目。" + "あ" * 50 + "</p>" for n in range(5))
        return httpx.Response(
            200, text=ARTICLE.format(t=title, body=body), headers={"content-type": "text/html"}
        )


def make(tmp_path: Path):
    conn = db.connect(":memory:")
    db.insert_feed(conn, "https://site.example/feed", "テスト", None, "tech/sub")
    cfg = Config(general=GeneralConfig(archive_dir=str(tmp_path / "archive")))
    return conn, cfg


def run(conn, cfg, site, **kw):
    feeds = db.list_feeds(conn, enabled=True)
    return asyncio.run(run_feeds(conn, feeds, cfg, transport=httpx.MockTransport(site.handler), **kw))


def test_first_run_marks_seen_and_backfills(tmp_path):
    conn, cfg = make(tmp_path)
    site = Site()
    [r] = run(conn, cfg, site, backfill=1)
    assert r.initialized
    assert [i.title for i in r.saved] == ["記事C"]  # 新しい順に 1 件
    statuses = dict(conn.execute("SELECT key, status FROM items").fetchall())
    assert statuses == {"a": "seen", "b": "seen", "c": "saved"}

    [path] = (tmp_path / "archive").rglob("*.md")
    assert path.relative_to(tmp_path / "archive").parts[:2] == ("tech", "sub")
    text = path.read_text()
    assert 'title: "記事C"' in text
    assert "source: article" in text
    assert "記事C の本文 0 段落目" in text
    assert text.count("# 記事C") == 1  # 本文側の同じ見出しは除く
    assert "メニュー" not in text


def test_second_run_saves_only_new_and_uses_etag(tmp_path):
    conn, cfg = make(tmp_path)
    site = Site()
    run(conn, cfg, site)

    # 変更なし → 304 で記事は取りに行かない
    site.requests.clear()
    [r] = run(conn, cfg, site)
    assert r.check.not_modified and site.requests == ["/feed"]

    site.items.append(("d", "記事D"))
    site.etag = '"v2"'
    [r] = run(conn, cfg, site)
    assert not r.initialized
    assert [i.title for i in r.saved] == ["記事D"]


def test_falls_back_to_feed_content(tmp_path):
    conn, cfg = make(tmp_path)
    site = Site()
    run(conn, cfg, site)
    site.items.append(("broken", "壊れた記事"))
    site.etag = '"v2"'
    [r] = run(conn, cfg, site)
    [item] = r.saved
    assert item.content_source == "feed"
    assert item.content_md == "要約 壊れた記事"


def _item(title: str | None, key: str = "k") -> Item:
    feed = db.Feed(
        id=1, url="u", original_url="u", title="F", site_url=None, category=None, enabled=True,
        disabled_reason=None, last_status=None, last_error=None, last_success_at=None,
        latest_entry_at=None, fail_count=0, etag=None, last_modified=None, initialized_at=None,
    )
    return Item(feed=feed, key=key, url=None, title=title, author=None,
                published=datetime(2026, 11, 7, tzinfo=UTC), feed_html=None)


def test_markdown_path_and_collision(tmp_path):
    now = datetime(2026, 11, 7, tzinfo=UTC)
    p1 = archive.save(tmp_path, _item('a/b: "c"?  d'), now)
    p2 = archive.save(tmp_path, _item('a/b: "c"?  d'), now)
    assert p1.parent.relative_to(tmp_path).parts == ("uncategorized", "2026", "11")
    assert p1.name.endswith("_a_b_c_d.md")
    assert p2.name.endswith("_a_b_c_d_2.md")
    assert archive.slugify("あ" * 100, "x") == "あ" * 50
    assert archive.slugify(None, "fallback") == "fallback"


def test_summarize_on_save(tmp_path):
    import json as _json
    import math

    from feedpipe.llm import Summarizer, clean_output

    calls = []

    def llm(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        calls.append(body)
        user = body["messages"][1]["content"]
        if body["max_tokens"] == 1:  # 重要度の判定: 記事C だけ A の確率が高い
            p_a = 0.9 if "記事C" in user else 0.2
            top = [{"token": "A", "logprob": math.log(p_a)}, {"token": "C", "logprob": math.log(1 - p_a)}]
            return httpx.Response(200, json={"choices": [{"message": {"content": "A"},
                                                          "logprobs": {"content": [{"top_logprobs": top}]}}]})
        title = user.split("\n")[0]
        return httpx.Response(200, json={"choices": [{"message": {"content": f"<think></think>{title} の要約。11 月 30 日まで"}}]})

    conn, cfg = make(tmp_path)
    site = Site()
    # 記事 A〜C は 2026/11/1〜3 公開。テスト実行日によらず「最近」と判定されるよう大きくする
    cfg.summarize.max_age_days = 100000

    async def go():
        async with Summarizer(cfg.summarize, transport=httpx.MockTransport(llm)) as s:
            feeds = db.list_feeds(conn, enabled=True)
            return await run_feeds(conn, feeds, cfg, backfill=2, summarizer=s,
                                   transport=httpx.MockTransport(site.handler))

    asyncio.run(go())
    judged = [c for c in calls if c["max_tokens"] == 1]
    summarized = [c for c in calls if c["max_tokens"] != 1]
    assert len(judged) == 2 and len(summarized) == 1  # 基準（0.8）以上の記事C だけ要約
    assert judged[0]["grammar"] == 'root ::= "A" | "B" | "C"'
    assert summarized[0]["chat_template_kwargs"] == {"enable_thinking": False}
    rows = dict(conn.execute("SELECT key, priority FROM items WHERE priority IS NOT NULL").fetchall())
    assert rows == {"b": 0.2, "c": 0.9}
    [summary] = [r[0] for r in conn.execute("SELECT summary FROM items WHERE summary IS NOT NULL")]
    assert summary == "タイトル: 記事Cの要約。11月30日まで"
    md = next(p for p in (tmp_path / "archive").rglob("*.md") if "記事C" in p.name).read_text()
    assert 'summary: "タイトル: 記事Cの要約。11月30日まで"' in md
    assert clean_output("AI 技術と 9 日") == "AI技術と9日"

    # 基準を 0 にすると判定せず全件要約する
    calls.clear()
    cfg.summarize.priority_threshold = 0
    from feedpipe.rss.pipeline import summarize_pending

    async def go2():
        async with Summarizer(cfg.summarize, transport=httpx.MockTransport(llm)) as s:
            return await summarize_pending(conn, s, cfg)

    assert asyncio.run(go2())["summarized"] == 1  # 残っていた記事B
    assert all(c["max_tokens"] != 1 for c in calls)


def test_summarize_skips_old_and_unavailable(tmp_path):
    from feedpipe.llm import Summarizer

    def down(request):
        raise httpx.ConnectError("refused")

    conn, cfg = make(tmp_path)

    async def go(days):
        cfg.summarize.max_age_days = days
        async with Summarizer(cfg.summarize, transport=httpx.MockTransport(down)) as s:
            return await run_feeds(conn, db.list_feeds(conn, enabled=True), cfg, backfill=3,
                                   summarizer=s, transport=httpx.MockTransport(Site().handler))

    # すべて古い扱い → 要約を呼ばない
    [r] = asyncio.run(go(0))
    assert len(r.saved) == 3 and all(i.summary is None for i in r.saved)

    # サーバーに繋がらなくても保存は成功する
    site = Site()
    site.items.append(("d", "記事D"))
    site.etag = '"v2"'

    async def go2():
        cfg.summarize.max_age_days = 100000
        async with Summarizer(cfg.summarize, transport=httpx.MockTransport(down)) as s:
            r = await run_feeds(conn, db.list_feeds(conn, enabled=True), cfg, summarizer=s,
                                transport=httpx.MockTransport(site.handler))
            return r, s.available

    [r], available = asyncio.run(go2())
    assert [i.title for i in r.saved] == ["記事D"] and r.saved[0].summary is None
    assert available is False


def test_classify_subgenre(tmp_path):
    import json as _json
    import math

    from feedpipe.llm import Summarizer

    (tmp_path / "genres.toml").write_text(
        '[tech]\nfeeds = ["https://site.example/feed"]\n'
        '[tech.ai]\nlabel = "AI"\ndescription = "生成 AI・機械学習"\n[tech.gadget]\nlabel = "ガジェット"\n',
        encoding="utf-8",
    )
    calls = []

    def llm(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        calls.append(body)
        user = body["messages"][1]["content"]
        if "Z" in body.get("grammar", ""):  # サブジャンルの判定: 記事C は B（ガジェット）、それ以外は Z
            best = "B" if "記事C" in user else "Z"
            top = [{"token": best, "logprob": math.log(0.8)}, {"token": "A", "logprob": math.log(0.2)}]
        else:  # 重要度の判定: すべて低い
            top = [{"token": "C", "logprob": math.log(0.9)}, {"token": "A", "logprob": math.log(0.1)}]
        return httpx.Response(200, json={"choices": [{"message": {"content": "A"},
                                                      "logprobs": {"content": [{"top_logprobs": top}]}}]})

    conn, cfg = make(tmp_path)
    conn.execute("UPDATE feeds SET category = 'tech'")
    cfg.general.genres_file = str(tmp_path / "genres.toml")
    cfg.summarize.max_age_days = 100000

    async def go():
        async with Summarizer(cfg.summarize, transport=httpx.MockTransport(llm)) as s:
            return await run_feeds(conn, db.list_feeds(conn), cfg, backfill=2, summarizer=s,
                                   transport=httpx.MockTransport(Site().handler))

    asyncio.run(go())
    classify = [c for c in calls if "Z" in c.get("grammar", "")]
    assert len(classify) == 2
    assert "A: AI（生成 AI・機械学習）" in classify[0]["messages"][0]["content"]
    assert classify[0]["grammar"] == 'root ::= "A" | "B" | "Z"'
    assert dict(conn.execute("SELECT key, subgenre FROM items WHERE status != 'seen'").fetchall()) == {
        "b": None,  # どれにも当てはまらない
        "c": "tech/gadget",
    }
