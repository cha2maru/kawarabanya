from pathlib import Path

from kawarabanya import db
from kawarabanya.rss import opml

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
    assert len(r.entries) == 13
    assert len(r.duplicates) == 1
