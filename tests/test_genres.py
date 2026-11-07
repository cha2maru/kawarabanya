import pytest

from kawarabanya.core import db
from kawarabanya.rss.genres import apply_genres, load_genres

TOML = """
[news]
label = "ニュース"
description = "総合"
feeds = ["https://a.example/rss"]

[news.world]
label = "国際"
feeds = ["https://b.example/rss"]

[tech]
feeds = ["https://moved.example/new", "https://unknown.example/rss"]
"""


def write(tmp_path, text):
    p = tmp_path / "genres.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load(tmp_path):
    g = load_genres(write(tmp_path, TOML))
    assert list(g) == ["news", "news/world", "tech"]
    assert g["news/world"].is_sub and g["news/world"].top == "news"
    assert g["tech"].label == "tech"  # label を省略するとキー名


@pytest.mark.parametrize(
    ("text", "msg"),
    [
        ('[a]\nfeeds=["u"]\n[b]\nfeeds=["u"]\n', "両方"),
        ("[a.b.c]\n", "サブジャンルの下"),
        ('[a]\nfeed = "typo"\n', "テーブル"),
    ],
)
def test_load_errors(tmp_path, text, msg):
    with pytest.raises(ValueError, match=msg):
        load_genres(write(tmp_path, text))


def test_apply(tmp_path):
    conn = db.connect(":memory:")
    a = db.insert_feed(conn, "https://a.example/rss", "A", None, "注目度1-100")
    b = db.insert_feed(conn, "https://b.example/rss", "B", None, "news")
    m = db.insert_feed(conn, "https://moved.example/old", "M", None, None)
    conn.execute("UPDATE feeds SET url = 'https://moved.example/new' WHERE id = ?", (m,))
    x = db.insert_feed(conn, "https://x.example/rss", "X", None, "old")

    r = apply_genres(conn, load_genres(write(tmp_path, TOML)), dry_run=True)
    assert db.get_feed(conn, a).category == "注目度1-100"  # dry-run は変えない

    r = apply_genres(conn, load_genres(write(tmp_path, TOML)))
    assert {f.id: new for f, new in r.moved} == {a: "news", b: "news/world", m: "tech"}
    assert [f.id for f in r.unassigned] == [x]
    assert r.unknown_urls == ["https://unknown.example/rss"]
    assert db.get_feed(conn, b).category == "news/world"
    assert db.get_feed(conn, x).category is None and db.get_feed(conn, x).enabled

    apply_genres(conn, load_genres(write(tmp_path, TOML)), disable_unassigned=True)
    assert db.get_feed(conn, x).disabled_reason == "no_genre"


def test_digest_flag(tmp_path):
    from kawarabanya.rss.genres import load_genres

    p = tmp_path / "genres.toml"
    p.write_text('[culture]\n[culture.sports]\nlabel = "スポーツ"\ndigest = false\n', encoding="utf-8")
    g = load_genres(p)
    assert (g["culture"].interest, g["culture/sports"].interest) == (None, "low")  # digest = false は low の古い書き方


def test_suggest_subgenres(tmp_path):
    import asyncio
    import json

    import httpx

    from kawarabanya.core import db
    from kawarabanya.core.config import SummarizeConfig
    from kawarabanya.core.llm import Summarizer
    from kawarabanya.rss import suggest
    from kawarabanya.rss.genres import load_genres

    p = tmp_path / "genres.toml"
    p.write_text('[news]\nlabel = "ニュース"\n[news.politics]\nlabel = "政治"\n[tech]\nlabel = "テック"\n',
                 encoding="utf-8")
    genres = load_genres(p)
    conn = db.connect(":memory:")
    feed = db.insert_feed(conn, "https://x.example/feed", None, None, "news")
    ids = {}
    with conn:
        for key, sub, status in [("a", "news", "notified"), ("b", "news", "saved"), ("c", "news", "saved"),
                                 ("d", "tech", "notified"), ("e", "tech", "saved"), ("f", "news/politics", "saved")]:
            ids[key] = db.insert_item(conn, feed, key, None, f"記事{key}", None, status)
            db.update_item(conn, ids[key], subgenre=sub)
    rows = suggest.unmatched_items(conn, genres, 7)
    assert sorted(r["title"] for r in rows) == ["記事a", "記事b", "記事c", "記事d", "記事e"]  # 当てはまったものは除く
    num = {r["title"]: n for n, r in enumerate(rows, 1)}
    answer = {"subgenres": [
        # 記事d は tech に投稿済みなので news には移せない → 3 件で採用
        {"genre": "news", "key": "weather", "label": "天気", "description": "天気・気象", "items": [num[f"記事{k}"] for k in "abcd"]},
        {"genre": "news", "key": "politics2", "label": "政治", "items": [1, 2, 3]},  # 既存と同じ名前
        {"genre": "nope", "key": "x", "label": "x", "items": [1, 2, 3]},  # ないジャンル
        {"genre": "tech", "key": "Bad Key", "label": "y", "items": [1, 2, 3]},
    ]}
    calls = []

    def llm(req):
        body = json.loads(req.content)
        calls.append(body)
        if "grammar" in body:  # 確かめの振り分け: 記事c だけ既存の「政治」（A）、ほかは新しいサブジャンル（B）
            best = "A" if "記事c" in body["messages"][1]["content"] else "B"
            top = [{"token": best, "logprob": -0.1}, {"token": "Z", "logprob": -2.4}]
            return httpx.Response(200, json={"choices": [{"message": {"content": best},
                                                          "logprobs": {"content": [{"top_logprobs": top}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})

    async def go():
        async with Summarizer(SummarizeConfig(), transport=httpx.MockTransport(llm)) as s:
            return await suggest.suggest(conn, s, genres)

    assert asyncio.run(go()) == []  # 確かめると 記事c は既存の「政治」を選ぶので、残り 2 件 → 採らない
    assert calls[0]["response_format"]["json_schema"]["schema"]["properties"]["subgenres"]["items"]["properties"]["genre"]["enum"] == ["news", "tech"]
    with conn:  # 記事e（tech の未投稿）も天気の記事だったことにする → a, b, e の 3 件で採る
        conn.execute("UPDATE items SET subgenre = 'news' WHERE id = ?", (ids["e"],))
    rows = suggest.unmatched_items(conn, genres, 7)
    num = {r["title"]: n for n, r in enumerate(rows, 1)}
    answer["subgenres"][0]["items"] = [num[f"記事{k}"] for k in "abcde"]
    [s] = asyncio.run(go())
    assert (s.path, s.label, sorted(s.item_ids)) == ("news/weather", "天気", sorted(ids[k] for k in "abe"))
    suggest.apply(conn, p, [s])
    g = load_genres(p)
    assert g["news/weather"].label == "天気" and g["news/weather"].description == "天気・気象"
    assert conn.execute("SELECT subgenre FROM items WHERE id = ?", (ids["b"],)).fetchone()[0] == "news/weather"


def test_merge_subgenres_over_limit(tmp_path, monkeypatch):
    import asyncio
    import json

    import httpx

    from kawarabanya.core import db
    from kawarabanya.core.config import SummarizeConfig
    from kawarabanya.core.llm import Summarizer
    from kawarabanya.rss import suggest
    from kawarabanya.rss.genres import load_genres

    monkeypatch.setattr(suggest, "MAX_TAGS", 4)  # 上限 4 を超える → 統合後は 3 個以下
    p = tmp_path / "genres.toml"
    p.write_text('[news]\nlabel = "ニュース"\n\n[news.a]\nlabel = "国内"\n\n[news.b]\nlabel = "地域"\ndigest = false\n\n'
                 '[news.c]\nlabel = "ボドゲ"\nfeeds = ["https://x.example/f"]\n\n# 末尾のコメント\n', encoding="utf-8")
    genres = load_genres(p)
    conn = db.connect(":memory:")
    feed = db.insert_feed(conn, "https://y.example/feed", None, None, "news")
    with conn:
        b = db.insert_item(conn, feed, "b", None, "地域の記事", None, "notified")
        db.update_item(conn, b, subgenre="news/b")
    new = [suggest.Suggestion("news", "d", "天気", "天気・気象", [b]), suggest.Suggestion("news", "e", "防災", "", [b])]
    answer = {"merges": [
        {"keys": ["a", "b"], "key": "a", "label": "国内・地域", "description": "国内と地域の出来事"},
        {"keys": ["d", "e"], "key": "d", "label": "天気・防災", "description": "天気と災害への備え"},
        {"keys": ["c", "a"], "key": "c", "label": "x"},  # 固定のサブジャンルはまとめない
    ]}

    def llm(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})

    async def go():
        async with Summarizer(SummarizeConfig(), transport=httpx.MockTransport(llm)) as s:
            return await suggest.plan_merges(s, genres, new)

    keep, merges = asyncio.run(go())
    assert [m.removed for m in merges] == [["news/b"], ["news/e"]]
    suggest.apply(conn, p, keep, merges)
    g = load_genres(p)
    assert [x.path for x in g.values() if x.is_sub] == ["news/a", "news/c", "news/d"]
    assert (g["news/a"].label, g["news/a"].description) == ("国内・地域", "国内と地域の出来事")
    assert (g["news/d"].label, g["news/d"].description) == ("天気・防災", "天気と災害への備え")
    assert g["news/c"].feeds == ["https://x.example/f"]
    assert conn.execute("SELECT subgenre FROM items WHERE id = ?", (b,)).fetchone()[0] == "news/d"

    answer["merges"] = []  # まとめきれない → 足さない
    keep, merges = asyncio.run(go())
    assert (keep, merges) == ([], [])


def test_edit_genres_toml(tmp_path):
    from kawarabanya.rss import genres

    p = tmp_path / "genres.toml"
    p.write_text('# 手書きのコメント\n[news]\nlabel = "ニュース"\nfeeds = [\n  "https://a.example/feed",  # A\n'
                 '  { url = "https://h.example/", item = "li" },\n]\n\n[news.world]\nlabel = "国際"\ndigest = false\n\n'
                 '[tech]\nlabel = "テック"\n', encoding="utf-8")
    genres.add_feed_url(p, "news", "https://b.example/rss", "B")
    genres.add_feed_url(p, "tech", "https://c.example/rss")  # feeds がなければ作る
    genres.set_interest(p, "news/world", "mute")  # digest = false を置き換える
    genres.set_interest(p, "tech", "like")
    g = genres.load_genres(p)
    assert g["news"].feeds == ["https://a.example/feed", "https://h.example/", "https://b.example/rss"]
    assert g["tech"].feeds == ["https://c.example/rss"]
    assert (g["news/world"].interest, g["tech"].interest) == ("mute", "like")
    assert genres.remove_feed_urls(p, ["https://a.example/feed", "https://h.example/"]) == 2
    genres.set_interest(p, "tech", "normal")  # normal は書かない
    g = genres.load_genres(p)
    assert g["news"].feeds == ["https://b.example/rss"] and g["tech"].interest is None
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# 手書きのコメント") and "digest" not in text and "# B" in text
