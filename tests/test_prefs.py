from datetime import UTC, datetime, timedelta

from kawarabanya.core import db
from kawarabanya.rss import digest, prefs


def test_words():
    assert prefs.words("Ｃｌｏｕｄｆｌａｒｅが新サービスを発表、AIとGPU 2026") == {"cloudflare", "サービス", "発表", "gpu"}
    assert prefs.words("第97回日本選手権、二回に2死満塁から先制。千葉県の一括") == {"先制", "千葉県", "一括"}


def setup():
    conn = db.connect(":memory:")
    a = db.insert_feed(conn, "https://a.example/rss", "A", None, "tech")
    b = db.insert_feed(conn, "https://b.example/rss", "B", None, "tech")
    now = datetime.now(UTC)
    t = (now - timedelta(hours=1)).isoformat(timespec="seconds")

    def add(i, feed, title, priority=0.5, likes=0, dislikes=0):
        conn.execute("INSERT INTO items (id, feed_id, key, title, fetched_at, notified_at, status, priority, likes, dislikes,"
                     " discord_thread_id) VALUES (?, ?, ?, ?, ?, ?, 'notified', ?, ?, ?, ?)",
                     (i, feed, str(i), title, t, t, priority, likes, dislikes, f"t{i}"))
    return conn, a, b, now, add


def test_learn_and_bonus():
    conn, a, b, now, add = setup()
    assert prefs.learn(conn, now, 60) == prefs.Prefs()  # 票がなければ空
    add(1, a, "Cloudflareの障害", likes=1)
    add(2, a, "Cloudflareの新機能", likes=1)
    add(3, b, "ガチャの話", dislikes=1)
    learned = prefs.learn(conn, now, 60)
    assert 0.285 < learned.words["cloudflare"] < 2 / 7  # 2 票 ÷ (2 件 + K=5)、少しだけ減衰
    assert learned.words["ガチャ"] < 0
    assert learned.sites[a] > 0 > learned.sites[b]
    feats = prefs.features("tech", "Cloudflareの値上げ", "")
    assert prefs.bonus(learned, a, feats, 0.2, 0.1) == 0.2 + 0.1  # それぞれの上限で頭打ち


def test_digest_prefs_are_capped():
    conn, a, b, now, add = setup()
    add(1, a, "Cloudflareの障害", priority=0.1, likes=3)
    add(2, b, "大事件", priority=0.9)
    add(3, a, "Cloudflareの値上げ", priority=0.3)
    learned = prefs.learn(conn, now, 60)
    ranked = digest.collect(conn, "tech", now - timedelta(hours=2), learned=learned)
    # 好みは +0.3 まで（特徴 0.2 + サイト 0.1）なので、重要度 0.9 の記事は抜けない
    assert [i.title for i in ranked] == ["大事件", "Cloudflareの値上げ", "Cloudflareの障害"]
    assert [i.base for i in ranked] == [0.9, 0.3, 0.1]


def test_explore_keeps_one_slot_without_prefs():
    items = [digest.DigestItem(t, None, None, None, base) for t, base in [("好み1", 0.1), ("好み2", 0.2), ("好み3", 0.1), ("重要", 0.8)]]
    assert [i.title for i in digest.explore(items, 3)] == ["好み1", "好み2", "重要", "好み3"]
    assert digest.explore(items, 5) == items


def test_review_and_ignore():
    conn, a, _, now, add = setup()
    add(1, a, "Cloudflareの加点", likes=1)
    learned = prefs.learn(conn, now, 60)
    assert {w for w, _ in prefs.to_review(conn, learned, 10)} == {"cloudflare", "加点"}
    assert prefs.ignore(conn, ["加点"]) == ["加点"]
    learned = prefs.learn(conn, now, 60)
    assert "加点" not in learned.words
    prefs.mark_reviewed(conn, ["cloudflare"], now)
    assert prefs.to_review(conn, learned, 10) == []
    assert prefs.last_review(conn) == now.replace(microsecond=0)
    prefs.ignore(conn, ["加点"], remove=True)
    assert prefs.ignored_words(conn) == set()
