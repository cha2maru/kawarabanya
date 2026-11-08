import asyncio
import socket

import httpx

from kawarabanya.core import db
from kawarabanya.core.config import GeneralConfig
from kawarabanya.rss.sources.check import Status, apply_results, check_feeds

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed T</title>
<item><title>a</title><link>https://x/a</link><pubDate>Mon, 02 Nov 2026 03:00:00 GMT</pubDate></item>
<item><title>b</title><link>https://x/b</link></item></channel></rss>"""


def handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "ok.example":
        return httpx.Response(200, content=RSS, headers={"content-type": "application/rss+xml"})
    if host == "moved.example":
        return httpx.Response(301, headers={"location": "https://ok.example/new"})
    if host == "temp.example":
        return httpx.Response(302, headers={"location": "https://ok.example/tmp"})
    if host == "dup.example":
        return httpx.Response(301, headers={"location": "https://ok.example/feed"})
    if host == "movedgone.example":
        return httpx.Response(301, headers={"location": "https://gone.example/new"})
    if host == "gone.example":
        return httpx.Response(404, text="nf")
    if host == "deny.example":
        return httpx.Response(403, text="no")
    if host == "html.example":
        return httpx.Response(200, text="<html><body>hi</body></html>", headers={"content-type": "text/html"})
    if host == "nxdomain.example":
        raise httpx.ConnectError("x") from socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    raise httpx.ConnectTimeout("timeout")


def setup():
    conn = db.connect(":memory:")
    urls = {
        "ok": "https://ok.example/feed",
        "moved": "http://moved.example/rss",
        "temp": "https://temp.example/rss",
        "dup": "https://dup.example/rss",
        "gone": "https://gone.example/rss",
        "deny": "https://deny.example/rss",
        "html": "https://html.example/",
        "nx": "https://nxdomain.example/rss",
        "slow": "https://slow.example/rss",
        "movedgone": "http://movedgone.example/rss",
    }
    ids = {k: db.insert_feed(conn, u, None, None, "c") for k, u in urls.items()}
    return conn, ids


def run(conn, **kw):
    feeds = db.list_feeds(conn, enabled=True)
    return asyncio.run(check_feeds(feeds, GeneralConfig(), transport=httpx.MockTransport(handler), **kw))


def test_classification():
    conn, ids = setup()
    by_id = {r.feed.id: r for r in run(conn)}
    st = {k: by_id[i].status for k, i in ids.items()}
    assert st == {
        "ok": Status.OK,
        "moved": Status.OK,
        "temp": Status.OK,
        "dup": Status.OK,
        "gone": Status.GONE,
        "deny": Status.FORBIDDEN,
        "html": Status.NOT_FEED,
        "nx": Status.DNS,
        "slow": Status.NETWORK,
        "movedgone": Status.GONE,
    }
    assert by_id[ids["movedgone"]].moved_to is None  # 移転先が 404 なら URL を変えない
    assert by_id[ids["ok"]].latest_entry.isoformat() == "2026-11-02T03:00:00+00:00"
    assert by_id[ids["ok"]].entries == 2
    assert by_id[ids["ok"]].feed_title == "Feed T"
    assert by_id[ids["moved"]].moved_to == "https://ok.example/new"
    assert by_id[ids["temp"]].moved_to is None  # 一時的なリダイレクトでは URL を変えない


def test_apply_results_updates_and_disables():
    conn, ids = setup()
    stats = apply_results(conn, run(conn), max_failures=5, disable_dead=True)
    assert stats.moved == 1
    assert stats.merged == 1
    assert stats.disabled == 4  # gone / movedgone / html / nx

    assert db.get_feed(conn, ids["moved"]).url == "https://ok.example/new"
    assert db.get_feed(conn, ids["moved"]).original_url == "http://moved.example/rss"
    assert db.get_feed(conn, ids["dup"]).disabled_reason == f"duplicate_of:{ids['ok']}"
    assert db.get_feed(conn, ids["ok"]).title == "Feed T"
    deny = db.get_feed(conn, ids["deny"])
    assert deny.enabled and deny.fail_count == 1  # 403 はすぐには無効化しない


def test_auto_disable_after_max_failures():
    conn, ids = setup()
    for _ in range(3):
        apply_results(conn, run(conn), max_failures=3)
    deny = db.get_feed(conn, ids["deny"])
    assert not deny.enabled
    assert deny.disabled_reason == "fail_x3:forbidden"
    assert db.get_feed(conn, ids["ok"]).fail_count == 0


def test_discover_feed():
    import asyncio

    import httpx

    from kawarabanya.core.config import GeneralConfig
    from kawarabanya.rss.sources.discover import discover

    rss = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Site</title><item><title>a</title><link>https://s.example/a</link></item></channel></rss>'

    def handler(req):
        if req.url.path == "/news":
            return httpx.Response(200, text='<html><head><link rel="alternate" type="application/rss+xml" href="/news/feed.xml"></head></html>')
        if req.url.path in ("/news/feed.xml", "/blog/index.rdf", "/f"):
            return httpx.Response(200, content=rss)
        return httpx.Response(404)

    t = httpx.MockTransport(handler)
    def run(u):
        return asyncio.run(discover(u, GeneralConfig(), transport=t))

    assert run("https://s.example/news") == ("https://s.example/news/feed.xml", "Site")  # ページ内の案内
    assert run("https://s.example/blog") == ("https://s.example/blog/index.rdf", "Site")  # よくある場所
    assert run("https://s.example/f") == ("https://s.example/f", "Site")  # フィードそのもの
    assert run("https://s.example/none") is None


def test_rsshub_match():
    from kawarabanya.rss.sources.rsshub import match

    rules = {
        "famitsu.com": {"_name": "ファミ通", "www": [
            {"title": "Category", "docs": "https://docs.rsshub.app/routes/famitsu",
             "source": ["/category/:category/page/1"], "target": "/famitsu/category/:category?"}]},
        "example.com": {".": [
            {"title": "Tag", "docs": "d", "source": ["/tag/:tag", "/tag/:tag/:page?x=1"], "target": "/ex/tag/:tag/:sort?"},
            {"title": "All", "docs": "d", "source": ["/*path"], "target": "/ex/all"}]},
    }
    [r] = match("https://www.famitsu.com/category/news/page/1", rules)
    assert (r.route, r.matched) == ("/famitsu/category/news", True)
    [r] = match("https://www.famitsu.com/ranking/", rules)  # ドメインは当たるがパスが違う
    assert (r.route, r.matched) == ("/famitsu/category/:category?", False)
    routes = match("https://example.com/tag/ai/", rules)
    assert [(x.route, x.matched) for x in routes] == [("/ex/tag/ai", True), ("/ex/all", True)]
    assert match("https://sub.example.com/tag/ai", rules) == []  # サブドメインが違う
    assert match("https://notexample.com/", rules) == []


def test_db_migrations(tmp_path, monkeypatch):
    import sqlite3

    import pytest

    from kawarabanya.core import db

    path = tmp_path / "k.db"
    conn = db.connect(path)
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    conn.close()

    # 開発中の古い DB（列が足りない・版 0）も版 1 にそろう
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, feed_id INTEGER, key TEXT, url TEXT, title TEXT,"
                  " published TEXT, fetched_at TEXT, status TEXT, md_path TEXT, summary TEXT, error TEXT)")
    conn = db.connect(old)
    assert "subgenre" in {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    conn.close()

    # 新しいマイグレーション: 既存の DB はバックアップしてから 1 回だけ適用する
    calls = []
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, ("列の追加", lambda c: calls.append(1)
                                                                or c.execute("ALTER TABLE feeds ADD COLUMN memo TEXT"))])
    conn = db.connect(path)
    assert calls == [1] and db.schema_version(conn) == len(db.MIGRATIONS)
    assert (tmp_path / f"k.db.bak-v{len(db.MIGRATIONS) - 1}").exists()
    conn.close()
    db.connect(path).close()
    assert calls == [1]

    # コードより新しい DB は開かない
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:1])
    with pytest.raises(ValueError, match="新しい"):
        db.connect(path)


def test_expand_url_days_ago():
    from datetime import date

    from kawarabanya.rss.sources.check import expand_url
    url = "http://rsshub/github/search/created:>{days_ago:7} stars:>300/stars/desc"
    assert expand_url(url, date(2026, 11, 8)) == "http://rsshub/github/search/created:>2026-11-01 stars:>300/stars/desc"
    assert expand_url("https://a.example/rss") == "https://a.example/rss"
