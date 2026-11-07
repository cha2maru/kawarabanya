import pytest

from feedpipe import db
from feedpipe.genres import apply_genres, load_genres

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
