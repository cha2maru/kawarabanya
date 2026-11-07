import asyncio
import time

import httpx
import pytest

from kawarabanya import db
from kawarabanya.config import GeneralConfig
from kawarabanya.rss.check import Status, apply_results, check_feeds
from kawarabanya.rss.genres import apply_genres, load_genres
from kawarabanya.rss.htmlsource import HtmlSource, extract, normalize_url

PAGE = """<html><head><title>お知らせ | Example</title></head><body>
<nav><a href="/about">会社概要</a></nav>
<ul class="news">
  <li class="row"><a href="/news/3?utm_source=top#top"><span class="t">新作 [予約]</span></a><time datetime="2026-11-05">11/5</time></li>
  <li class="row"><a href="https://example.com/news/2"><span class="t">発売日決定</span></a><span class="d">2026年10月21日</span></li>
  <li class="row"><a href="/news/3">重複</a></li>
  <li class="row"><a href="/shop/item/9"><span class="t">商品</span></a></li>
  <li class="row"><span class="t">リンクなし</span></li>
</ul>
<div class="card"><h5>カード記事</h5><span class="text-muted">他1品</span><div class="date">2026.09.30</div>
  <a class="stretched-link" href="/stories/1"></a></div>
</body></html>"""


def test_extract_list_items():
    es = extract(PAGE, "https://example.com/news", HtmlSource(item="ul.news li", title=".t", date="time, .d"))
    assert [e["link"] for e in es] == [
        "https://example.com/news/3",  # 追跡用パラメータと # 以降を除き、相対 URL を解決
        "https://example.com/news/2",
        "https://example.com/shop/item/9",
    ]  # 同じ URL は 1 件、リンクのない塊は飛ばす
    assert [e["title"] for e in es] == ["新作 [予約]", "発売日決定", "商品"]
    assert time.strftime("%Y-%m-%d %H", es[1]["published_parsed"]) in ("2026-10-20 15", "2026-10-21 00")  # JST 0 時
    assert es[2]["published_parsed"] is None


def test_extract_with_link_pattern_and_card():
    es = extract(PAGE, "https://example.com/", HtmlSource(item="ul.news li", link_pattern=r"/news/\d+$"))
    assert [e["title"] for e in es] == ["新作 [予約]", "発売日決定"]  # title 省略時はリンクの文字列
    [card] = extract(PAGE, "https://example.com/", HtmlSource(item=".card", title="h5", link="a.stretched-link",
                                                              date=".date"))
    assert (card["title"], card["link"]) == ("カード記事", "https://example.com/stories/1")
    assert card["published_parsed"] is not None


def test_normalize_url():
    assert normalize_url("https://a.example/p?id=1&utm_medium=x&fbclid=y#c") == "https://a.example/p?id=1"


def test_genres_html_entry_registers_source(tmp_path):
    p = tmp_path / "genres.toml"
    p.write_text('[hobby]\nfeeds = [\n  "https://rss.example/feed",\n'
                 '  { url = "https://example.com/news", name = "Example", item = "ul.news li", title = ".t" },\n]\n',
                 encoding="utf-8")
    conn = db.connect(":memory:")
    db.insert_feed(conn, "https://rss.example/feed", "RSS", None, None)
    r = apply_genres(conn, load_genres(p))
    assert r.added == ["https://example.com/news"]
    feed = db.find_feed_by_url(conn, "https://example.com/news")
    assert (feed.source_type, feed.title, feed.category) == ("html", "Example", "hobby")
    assert feed.option("html") == {"item": "ul.news li", "title": ".t"}
    assert apply_genres(conn, load_genres(p)).added == []  # 2 回目は登録済み

    p.write_text('[hobby]\nfeeds = [{ url = "https://x.example/", title = "h2" }]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="item"):
        load_genres(p)
    p.write_text('[hobby]\nfeeds = [{ url = "https://x.example/", item = "li", titel = "h2" }]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="titel"):
        load_genres(p)


def test_check_html_source_and_broken_selector():
    conn = db.connect(":memory:")
    ok = db.insert_feed(conn, "https://example.com/news", None, None, "hobby")
    broken = db.insert_feed(conn, "https://example.com/old", None, None, "hobby")
    for fid, sel in [(ok, "ul.news li"), (broken, "div.no-such-list li")]:
        conn.execute("UPDATE feeds SET source_type = 'html' WHERE id = ?", (fid,))
        db.set_option(conn, fid, "html", {"item": sel, "title": ".t"})

    def handler(req):
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    results = asyncio.run(check_feeds(db.list_feeds(conn), GeneralConfig(), transport=httpx.MockTransport(handler)))
    by_id = {r.feed.id: r for r in results}
    assert by_id[ok].status is Status.OK and by_id[ok].entries == 3
    assert by_id[broken].status is Status.NOT_FEED and "取り出せません" in by_id[broken].error
    apply_results(conn, results, max_failures=5)
    assert db.get_feed(conn, ok).title == "お知らせ | Example"  # 名前がなければページのタイトル
    assert db.get_feed(conn, broken).fail_count == 1  # 0 件は失敗として数える


def test_extract_shift_jis_page():
    html = '<html><head><meta charset="Shift_JIS"></head><body><li><a href="/n/1">日本語の記事</a></li></body></html>'
    [e] = extract(html.encode("shift_jis"), "https://example.com/", HtmlSource(item="li"))
    assert e["title"] == "日本語の記事"


def test_decode_html_charsets():
    from kawarabanya.rss.htmlsource import decode_html

    body = "<p>置き配を盗まれたのでダミー荷物を置いてみた結果、見事にまた盗まれた</p>" * 3
    euc = f'<html><head><meta charset="EUC-JP"></head><body>{body}</body></html>'.encode("euc_jp")
    assert decode_html(euc, "EUC-JP")[1] == "euc-jp"  # HTTP ヘッダー
    assert "置き配" in decode_html(euc)[0]  # <meta charset>
    assert "置き配" in decode_html(euc, "iso-8859-1")[0]  # ヘッダーの latin-1 は信じない
    nometa = f"<html><body>{body}</body></html>".encode("shift_jis")
    assert "置き配" in decode_html(nometa)[0]  # 指定がなければ中身から推定
    assert "置き配" in decode_html(nometa, "utf-8")[0]  # ヘッダーが間違っていても推定する
    # EUC-JP のページに UTF-8 が少し混ざっていても EUC-JP として読む
    mixed = euc.replace(b"<body>", b"<body><!--" + "広告".encode() + b"-->") + body.encode("euc_jp") * 20
    text, enc = decode_html(mixed, "euc-jp")
    assert enc == "euc-jp" and "置き配" in text


def test_extension_source(tmp_path, monkeypatch):
    import json

    from kawarabanya.rss import extensions

    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir()
    (ext_dir / "jsonapi.py").write_text(
        "import json\n"
        "def validate(options):\n"
        "    if 'key' not in options:\n"
        "        raise ValueError('key が必要です')\n"
        "def parse(content, url, options):\n"
        "    return [{'id': x['u'], 'link': x['u'], 'title': x['t'], 'published_parsed': None, 'summary': ''}\n"
        "            for x in json.loads(content)[options['key']]]\n"
        "def title(content):\n"
        "    return 'JSON のサイト'\n", encoding="utf-8")
    (ext_dir / "broken.py").write_text("def parse(content, url, options):\n    raise RuntimeError('壊れた')\n",
                                       encoding="utf-8")
    monkeypatch.setattr(extensions, "DIR", ext_dir)
    monkeypatch.setattr(extensions, "_loaded", dict(extensions._loaded))
    assert extensions.available() == ["html", "sitemap", "broken", "jsonapi"]

    p = tmp_path / "genres.toml"
    p.write_text('[news]\nfeeds = [\n  { url = "https://api.example/list", extension = "jsonapi", key = "items" },\n'
                 '  { url = "https://b.example/", extension = "broken" },\n]\n', encoding="utf-8")
    conn = db.connect(":memory:")
    apply_genres(conn, load_genres(p))
    feed = db.find_feed_by_url(conn, "https://api.example/list")
    assert (feed.source_type, feed.option("jsonapi")) == ("jsonapi", {"key": "items"})

    def handler(req):
        return httpx.Response(200, content=json.dumps({"items": [{"u": "https://api.example/1", "t": "一つ目"}]}).encode())

    results = asyncio.run(check_feeds(db.list_feeds(conn), GeneralConfig(), transport=httpx.MockTransport(handler)))
    by_url = {r.feed.url: r for r in results}
    ok = by_url["https://api.example/list"]
    assert ok.status is Status.OK and ok.feed_title == "JSON のサイト" and ok.parsed_entries[0]["title"] == "一つ目"
    bad = by_url["https://b.example/"]
    assert bad.status is Status.NOT_FEED and "壊れた" in bad.error  # 拡張の例外は、そのフィードの失敗になる

    p.write_text('[news]\nfeeds = [{ url = "https://api.example/list", extension = "jsonapi" }]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="key が必要"):
        load_genres(p)
    p.write_text('[news]\nfeeds = [{ url = "https://x.example/", extension = "nope" }]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="拡張 nope がありません"):
        load_genres(p)
