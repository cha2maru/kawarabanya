from datetime import UTC, datetime, timedelta

import httpx

from kawarabanya.core import db
from kawarabanya.rss import digest, trends

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>熊本県</title><ht:approx_traffic>2000+</ht:approx_traffic></item>
<item><title>sg</title><ht:approx_traffic>200+</ht:approx_traffic></item>
<item><title>高校サッカー 埼玉</title><ht:approx_traffic>100+</ht:approx_traffic></item>
</channel></rss>""".encode()


def test_parse_and_bonus():
    words = trends.parse(RSS)
    assert words == {"熊本県": 2000, "高校サッカー 埼玉": 100}  # 短い英字は使わない
    assert round(trends.trend_bonus("熊本県で震度4", words), 2) == 0.23
    assert trends.trend_bonus("埼玉の高校サッカー決勝", words) == 0.1  # 語はすべて含めば順不同
    assert trends.trend_bonus("埼玉の話", words) == 0.0
    assert trends.trend_bonus("熊本", {"熊本": 10**9}) == trends.MAX_BONUS


def test_record_keeps_24_hours():
    conn = db.connect(":memory:")
    now = datetime(2026, 11, 8, tzinfo=UTC)
    trends.record(conn, {"古い": 100}, now - timedelta(hours=25))
    trends.record(conn, {"熊本県": 2000}, now - timedelta(hours=1))
    trends.record(conn, {"熊本県": 100, "新しい": 500}, now)
    assert trends.load(conn, now) == {"熊本県": 2000, "新しい": 500}
    client = httpx.MockTransport(lambda req: httpx.Response(200, content=RSS))
    # 取った 2 語が、テストの固定日時（now）でためた語に加わる（固定日時の語は実際の今からは古ければ消える）
    assert trends.fetch(conn, "JP", "ua", transport=client) >= 2


def test_digest_puts_buzz_first(tmp_path):
    conn = db.connect(":memory:")
    feeds = [db.insert_feed(conn, f"https://s{i}.example/rss", f"S{i}", None, "news") for i in range(3)]
    t = datetime.now(UTC).isoformat(timespec="seconds")

    def add(i, feed, title, status="notified", priority=0.5, error=None):
        conn.execute("INSERT INTO items (id, feed_id, key, title, fetched_at, notified_at, status, priority, error)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (i, feed, str(i), title, t, t, status, priority, error))

    add(1, feeds[0], "ふつう", priority=0.6)
    add(2, feeds[0], "3 サイトが報じた")
    add(3, feeds[1], "同じ", "duplicate", error="item 2")
    add(4, feeds[2], "同じ", "duplicate", error="same story: item 2")
    add(5, feeds[0], "熊本県で震度4")
    since = datetime.now(UTC) - timedelta(hours=1)
    assert trends.source_counts(conn, since) == {2: 3}
    titles = [i.title for i in digest.collect(conn, "discord", "news", since, words={"熊本県": 2000})]
    assert titles == ["熊本県で震度4", "3 サイトが報じた", "ふつう"]  # 0.5+0.23、0.5+0.2、0.6
