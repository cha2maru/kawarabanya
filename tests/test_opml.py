from pathlib import Path

from kawarabanya.core import db
from kawarabanya.rss.sources import opml

NESTED = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head><title>t</title></head>
  <body>
    <outline text="tech" title="tech">
      <outline type="rss" text="A &amp; B" xmlUrl="https://a.example/feed?x=1&amp;y=2" htmlUrl="https://a.example/"/>
      <outline text="sub">
        <outline type="rss" text="C" xmlUrl="https://c.example/rss"/>
      </outline>
    </outline>
    <outline text="news">
      <outline type="rss" text="A dup" xmlUrl="https://a.example/feed?x=1&amp;y=2"/>
    </outline>
    <outline type="rss" text="Top" xmlUrl="https://top.example/atom"/>
  </body>
</opml>
"""


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "in.opml"
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_nested_and_duplicates(tmp_path):
    r = opml.parse_opml(write(tmp_path, NESTED))
    assert [(e.url, e.category, e.title) for e in r.entries] == [
        ("https://a.example/feed?x=1&y=2", "tech", "A & B"),
        ("https://c.example/rss", "tech/sub", "C"),
        ("https://top.example/atom", None, "Top"),
    ]
    assert r.entries[0].site_url == "https://a.example/"
    assert r.duplicates == [("https://a.example/feed?x=1&y=2", "news")]


def test_import_is_idempotent_and_update(tmp_path):
    conn = db.connect(":memory:")
    entries = opml.parse_opml(write(tmp_path, NESTED)).entries
    s1 = opml.import_entries(conn, entries)
    s2 = opml.import_entries(conn, entries)
    assert (s1.added, s1.skipped) == (3, 0)
    assert (s2.added, s2.skipped) == (0, 3)

    entries[0].category = "moved"
    s3 = opml.import_entries(conn, entries, update=True)
    assert s3.updated == 3
    assert db.find_feed_by_url(conn, entries[0].url).category == "moved"


def test_export_roundtrip(tmp_path):
    conn = db.connect(":memory:")
    opml.import_entries(conn, opml.parse_opml(write(tmp_path, NESTED)).entries)
    out = tmp_path / "out.opml"
    opml.export_opml(db.list_feeds(conn), out)
    again = opml.parse_opml(out)
    assert sorted((e.url, e.category, e.title) for e in again.entries) == sorted(
        (f.url, f.category, f.title) for f in db.list_feeds(conn)
    )


def test_sample_opml():
    sample = Path(__file__).parent.parent / "sample.opml"
    r = opml.parse_opml(sample)
    assert len(r.entries) == 11
    assert len(r.duplicates) == 1  # GIGAZINE は 2 つのフォルダにある
    assert {e.category for e in r.entries} == {"news", "tech", "tech/dev", "game", "culture"}  # 入れ子のフォルダ


def test_import_opml_writes_genres(tmp_path, capsys):
    import argparse

    from kawarabanya.cli.rss import cmd_import_opml
    from kawarabanya.core.config import Config, GeneralConfig
    from kawarabanya.rss.genres import load_genres

    g = tmp_path / "genres.toml"
    g.write_text('[news]\nlabel = "ニュース"\n\n[tech]\nlabel = "テック"\nfeeds = [\n  "https://gigazine.net/news/rss_2.0/",\n]\n',
                 encoding="utf-8")
    cfg = Config(general=GeneralConfig(db=str(tmp_path / "k.db"), genres_file=str(g)))
    sample = Path(__file__).parent.parent / "sample.opml"
    args = argparse.Namespace(file=sample, update=False, verbose=False, genre=None, db=None)
    assert cmd_import_opml(cfg, args) == 0
    defs = load_genres(g)
    assert len(defs["news"].feeds) == 4  # フォルダ news → ジャンル news
    assert len(defs["tech"].feeds) == 4  # tech と tech/dev → tech（GIGAZINE は書いてあるので足さない）
    assert "3 件は、genres.toml に書くまで" in capsys.readouterr().out  # game・culture のジャンルはない

    conn = db.connect(tmp_path / "k.db")
    assert conn.execute("SELECT category FROM feeds WHERE url LIKE '%zenn%'").fetchone()[0] == "tech"
    args.genre = "nope"
    assert cmd_import_opml(cfg, args) == 1
