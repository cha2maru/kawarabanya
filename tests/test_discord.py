import json
from datetime import UTC, datetime, timedelta

import httpx

from kawarabanya.core import db, discord_api
from kawarabanya.core.discord_api import GUILD_FORUM, DiscordClient, channel_name
from kawarabanya.rss import notify
from kawarabanya.rss.forums import apply_plan, feeds_by_category, make_plan


class FakeDiscord:
    """フォーラム・タグ・Webhook の作成と、Webhook への投稿を真似る。"""

    def __init__(self):
        self.channels = [
            {"id": "10", "type": 4, "name": "kawarabanya"},
            {"id": "11", "type": GUILD_FORUM, "name": "tech", "parent_id": "10",
             "available_tags": [{"id": "t1", "name": "AI"}, {"id": "t2", "name": "既存フィード"}]},
            {"id": "12", "type": GUILD_FORUM, "name": "news"},  # 親カテゴリが違うので別物
        ]
        self.webhooks: dict[str, list] = {"11": [{"id": "w11", "name": "kawarabanya", "token": "k11"}]}
        self.posts: list[dict] = []
        self.likes: dict[str, int] = {}
        self.messages: dict[str, dict] = {}  # Webhook で投稿したメッセージ
        self.thread_tags: dict[str, list] = {}
        self.deleted: list[str] = []
        self.next_id = 100
        self.rate_limited_once = False

    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v10")
        body = json.loads(req.content) if req.content else None
        if req.url.path.startswith("/api/webhooks/") and "/messages/" in req.url.path:  # 投稿の編集・削除
            mid = req.url.path.rsplit("/", 1)[1]
            if mid not in self.messages:
                return httpx.Response(404, json={"message": "Unknown Message"})
            if req.method == "DELETE":
                del self.messages[mid]
                return httpx.Response(204)
            self.messages[mid] = body
            return httpx.Response(200, json={"id": mid, **body})
        if req.url.path.startswith("/api/webhooks/"):  # Webhook URL はバージョンなし・認証なし
            if not self.rate_limited_once:
                self.rate_limited_once = True
                return httpx.Response(429, json={"retry_after": 0.01})
            self.posts.append(body)
            mid = self._id()
            self.messages[mid] = body
            return httpx.Response(200, json={"id": mid, "channel_id": f"thread{len(self.posts)}"})
        assert req.headers["authorization"] == "Bot TOKEN"
        if path == "/users/@me/guilds":
            return httpx.Response(200, json=[{"id": "1", "name": "srv"}])
        if path == "/guilds/1/channels" and req.method == "GET":
            return httpx.Response(200, json=self.channels)
        if path == "/guilds/1/channels" and req.method == "POST":
            tags = [{"id": self._id(), **t} for t in body.pop("available_tags", [])]
            ch = {"id": self._id(), **body, "available_tags": tags}
            self.channels.append(ch)
            return httpx.Response(201, json=ch)
        if req.method == "DELETE" and path.startswith("/channels/thread"):  # フォーラムの投稿を消す
            if path in self.deleted:
                return httpx.Response(404, json={"message": "Unknown Channel"})
            self.deleted.append(path)
            return httpx.Response(200, json={})
        if req.method == "PATCH" and path.startswith("/channels/"):
            cid = path.split("/")[2]
            if cid.startswith("thread"):  # フォーラムの投稿（スレッド）のタグの書き換え
                self.thread_tags[cid] = body["applied_tags"]
                return httpx.Response(200, json={"id": cid, **body})
            ch = next(c for c in self.channels if c["id"] == cid)
            if "available_tags" in body:
                ch["available_tags"] = [t if "id" in t else {"id": self._id(), **t} for t in body["available_tags"]]
            if "nsfw" in body:
                ch["nsfw"] = body["nsfw"]
            if "default_reaction_emoji" in body:
                ch["default_reaction_emoji"] = body["default_reaction_emoji"]
            return httpx.Response(200, json=ch)
        if "/messages/" in path:  # フォーラム投稿の最初のメッセージ
            tid = path.split("/")[2]
            if tid not in self.likes:
                return httpx.Response(404, json={"message": "Unknown Message"})
            return httpx.Response(200, json={"id": tid, "reactions": [
                {"emoji": {"id": None, "name": "👍"}, "count": self.likes[tid]},
                {"emoji": {"id": None, "name": "👎"}, "count": 1},
                {"emoji": {"id": None, "name": "🎉"}, "count": 9},
            ]})
        if path.endswith("/webhooks"):
            cid = path.split("/")[2]
            if req.method == "GET":
                return httpx.Response(200, json=self.webhooks.get(cid, []))
            hook = {"id": f"w{cid}", "name": body["name"], "token": f"k{cid}"}
            self.webhooks.setdefault(cid, []).append(hook)
            return httpx.Response(200, json=hook)
        return httpx.Response(404, json={"message": "not found"})


def ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat(timespec="seconds")


def setup():
    fake = FakeDiscord()
    client = DiscordClient("TOKEN", transport=httpx.MockTransport(fake.handler))
    conn = db.connect(":memory:")
    for title, cat in [("既存フィード", "tech"), ("新フィード", "tech"), ("ニュース", "news"), ("ホビー誌", "ホビー")]:
        db.insert_feed(conn, f"https://{len(title)}{cat}.example/{title}", title, None, cat)
    return fake, client, conn


def tech_genres(tmp_path):
    from kawarabanya.rss.genres import load_genres

    g = tmp_path / "genres.toml"
    g.write_text('[tech]\n[tech.ai]\nlabel = "AI"\n[tech.security]\nlabel = "セキュリティ"\n', encoding="utf-8")
    return load_genres(g)


def test_channel_name():
    assert channel_name("tech") == "tech"
    assert channel_name("News Daily/Sub") == "news-daily-sub"
    assert channel_name("注目度1-100") == "注目度1-100"
    assert channel_name("LifeHackパック") == "lifehackパック"
    assert channel_name(None) == "uncategorized"


def test_plan_and_apply_forums(tmp_path):
    fake, client, conn = setup()
    guild = discord_api.resolve_guild(client, None)
    plan = make_plan(client, guild, feeds_by_category(conn), "kawarabanya", tech_genres(tmp_path))
    assert plan.parent_id == "10"
    assert [(p.name, p.exists) for p in plan.channels] == [("news", False), ("tech", True), ("ホビー", False)]
    tech = plan.channels[1]
    # タグはサブジャンルだけ。フィード名の古いタグは消す
    assert tech.wanted_tags() == ["AI", "セキュリティ"]
    assert tech.missing_tags() == ["セキュリティ"]
    assert [t["name"] for t in tech.extra_tags()] == ["既存フィード"]

    created = apply_plan(client, conn, plan, "kawarabanya", nsfw=True)
    assert [p.name for p in created] == ["news", "ホビー"]
    hobby = next(c for c in fake.channels if c["name"] == "ホビー")
    assert hobby["type"] == GUILD_FORUM and hobby["parent_id"] == "10"
    assert hobby["nsfw"] is True
    assert hobby["available_tags"] == []  # サブジャンルのないジャンルはタグなし
    tech_ch = next(c for c in fake.channels if c["id"] == "11")
    assert [t["name"] for t in tech_ch["available_tags"]] == ["AI", "セキュリティ"]
    assert tech_ch["available_tags"][0]["id"] == "t1"  # 残すタグは id を保つ
    assert tech_ch["nsfw"] is True  # 既存のフォーラムも年齢制限にそろえる

    rows = {r["category"]: r for r in conn.execute("SELECT * FROM discord_channels")}
    assert rows["tech"]["webhook_url"] == "https://discord.com/api/webhooks/w11/k11"  # 既存を再利用
    assert set(json.loads(rows["tech"]["tags"])) == {"sub:tech/ai", "sub:tech/security"}

    # もう一度実行しても作り直さない
    again = make_plan(client, guild, feeds_by_category(conn), "kawarabanya", tech_genres(tmp_path))
    assert all(p.exists and not p.missing_tags() and not p.extra_tags() and p.nsfw for p in again.channels)
    assert apply_plan(client, conn, again, "kawarabanya", nsfw=True) == []


def test_notify_posts_forum_threads(tmp_path):
    fake, client, conn = setup()
    plan = make_plan(client, {"id": "1", "name": "srv"}, feeds_by_category(conn), "kawarabanya",
                     tech_genres(tmp_path))
    apply_plan(client, conn, plan, "kawarabanya")

    md = tmp_path / "a.md"
    md.write_text('---\ntitle: "x"\n---\n\n# 記事1\n\n本文の **先頭** です。[リンク](https://l) 20\\~50% a\\_b\n\n---\n\n元記事: <https://a>\n')
    feed_id = db.find_feed_by_url(conn, "https://6tech.example/既存フィード").id
    with conn:
        a = db.insert_item(conn, feed_id, "a", "https://a", "記事1", ago(hours=5), "saved")
        db.update_item(conn, a, md_path=str(md), subgenre="tech/ai")  # LLM が判定したサブジャンル
        b = db.insert_item(conn, feed_id, "b", "https://b", "記事2", ago(hours=1), "saved")
        db.update_item(conn, b, summary="LLM の要約 @everyone")
        db.insert_item(conn, feed_id, "c", "https://c", "既読", None, "seen")
        db.insert_item(conn, feed_id, "d", "https://d", "古い記事", ago(days=4), "saved")

    stats = notify.notify_pending(
        conn, limit=10, max_age_days=3, transport=httpx.MockTransport(fake.handler), interval=0
    )
    assert (stats.sent, stats.failed, stats.stale) == (2, 0, 1)
    p1, p2 = fake.posts
    assert p1["thread_name"] == "記事1"
    assert p1["username"] == "既存フィード"
    assert p1["applied_tags"] == ["t1"]  # 記事のサブジャンル（AI）のタグ
    assert "applied_tags" not in p2  # サブジャンルが決まらなかった記事はタグなし
    assert p1["content"].startswith("本文の 先頭 です。リンク 20~50% a_b")
    assert "🕒 " in p1["content"]
    assert p1["content"].endswith("https://a")
    assert p2["content"].startswith("LLM の要約")
    assert p2["allowed_mentions"] == {"parse": []}

    rows = dict(conn.execute("SELECT key, status FROM items").fetchall())
    assert rows == {"a": "notified", "b": "notified", "c": "seen", "d": "stale"}
    again = notify.notify_pending(conn, limit=10, max_age_days=3, transport=httpx.MockTransport(fake.handler))
    assert (again.sent, again.stale) == (0, 0)


def test_notify_limit_and_missing_channel():
    _, _, conn = setup()
    feed_id = db.list_feeds(conn)[0].id
    with conn:
        db.insert_item(conn, feed_id, "x", "https://x", "x", None, "saved")
    stats = notify.notify_pending(conn, limit=10, max_age_days=3)
    assert stats.no_channel == 1 and stats.sent == 0


def test_subgenre_tags(tmp_path):
    from kawarabanya.rss.genres import load_genres

    fake = FakeDiscord()
    fake.channels = []
    client = DiscordClient("TOKEN", transport=httpx.MockTransport(fake.handler))
    conn = db.connect(":memory:")
    fixed = db.insert_feed(conn, "https://w.example/rss", "世界ニュース", None, "news/world")  # サブジャンルに固定
    top = db.insert_feed(conn, "https://d.example/rss", "国内ニュース", None, "news")
    g = tmp_path / "genres.toml"
    g.write_text('[news]\ndescription = "総合"\n[news.world]\nlabel = "国際"\n', encoding="utf-8")

    plan = make_plan(client, {"id": "1", "name": "srv"}, feeds_by_category(conn), "kawarabanya",
                     load_genres(g))
    [pc] = plan.channels  # news/world も news のフォーラムにまとまる
    assert pc.name == "news" and pc.wanted_tags() == ["国際"]
    apply_plan(client, conn, plan, "kawarabanya")
    forum = next(c for c in fake.channels if c["type"] == GUILD_FORUM)
    assert forum["topic"] == "総合"

    with conn:
        db.insert_item(conn, fixed, "k", "https://w.example/1", "固定", ago(hours=2), "saved")
        i = db.insert_item(conn, top, "m", "https://d.example/1", "判定", ago(hours=1), "saved")
        db.update_item(conn, i, subgenre="news/world")
    notify.notify_pending(conn, limit=5, max_age_days=3, transport=httpx.MockTransport(fake.handler), interval=0)
    [tag] = [t["id"] for t in forum["available_tags"]]
    assert [p["applied_tags"] for p in fake.posts] == [[tag], [tag]]


def test_default_reaction_and_like_sync():
    fake, client, conn = setup()
    plan = make_plan(client, {"id": "1", "name": "srv"}, feeds_by_category(conn), "kawarabanya")
    apply_plan(client, conn, plan, "kawarabanya")
    forums = [c for c in fake.channels if c["type"] == GUILD_FORUM and c.get("parent_id") == "10"]
    assert all(c["default_reaction_emoji"]["emoji_name"] == "👍" for c in forums)  # 新規も既存も

    feed_id = db.list_feeds(conn)[0].id
    with conn:
        ids = {}
        for key, tid in [("a", "t-a"), ("b", "t-b"), ("gone", "t-gone")]:
            ids[key] = db.insert_item(conn, feed_id, key, None, key, ago(hours=1), "notified")
            db.update_item(conn, ids[key], discord_thread_id=tid, notified_at=ago(hours=1))
    fake.likes = {"t-a": 2, "t-b": 0}

    assert notify.sync_likes(conn, client, interval_hours=6) == 3
    rows = conn.execute("SELECT key, likes, dislikes FROM items WHERE key IN ('a','b','gone')").fetchall()
    assert {r[0]: (r[1], r[2]) for r in rows} == {"a": (2, 1), "b": (0, 1), "gone": (None, None)}
    assert notify.sync_likes(conn, client, interval_hours=6) == 0  # 6 時間以内は再確認しない
    assert notify.sync_likes(conn, client, interval_hours=0) == 3


def test_digest_render_and_post(tmp_path):
    import asyncio
    from datetime import datetime

    from kawarabanya.rss import digest

    fake, client, conn = setup()
    plan = make_plan(client, {"id": "1", "name": "srv"}, feeds_by_category(conn, ["tech"]), "kawarabanya")
    apply_plan(client, conn, plan, "kawarabanya")
    feed_id = db.list_feeds(conn, category="tech")[0].id
    with conn:
        for key, title, prio, likes in [("a", "小ネタ", 0.1, None), ("b", "大事件 [速報]", 0.95, 2), ("c", "判定なし", None, None)]:
            iid = db.insert_item(conn, feed_id, key, None, title, ago(hours=2), "notified")
            db.update_item(conn, iid, notified_at=ago(hours=2), discord_thread_id=f"t-{key}", priority=prio, likes=likes)

    fake.channels.append({"id": "50", "type": 0, "name": "ニュース", "parent_id": "999"})  # kawarabanya の外
    ch = discord_api.find_text_channel(client, "1", "ニュース")
    discord_api.register_webhook(client, conn, discord_api.DIGEST_KEY, "1", ch["id"], ch["name"], "kawarabanya")

    now = datetime.now(UTC)
    content = asyncio.run(digest.post_digest(conn, None, "tech", label="テック", now=now, top=2, window_hours=24,
                                               transport=httpx.MockTransport(fake.handler)))
    lines = content.splitlines()
    assert lines[0].startswith("## 📰 テック ") and lines[0].endswith("のまとめ（3 件）")
    assert lines[1] == "**注目の記事**"
    assert lines[2] == "- [大事件 ［速報］](https://discord.com/channels/1/t-b)  👍2"  # 重要度の高い順
    assert lines[3] == "- [小ネタ](https://discord.com/channels/1/t-a)"
    assert lines[4] == "ほか 1 件"
    assert "thread_name" not in fake.posts[-1]  # テキストチャンネルへの普通のメッセージ
    t = httpx.MockTransport(fake.handler)
    # 定時（due_after）より後に投稿済みなら投稿しない。前回のまとめ以降の記事がなければ、指示されても投稿しない
    slot = now - timedelta(minutes=10)
    assert asyncio.run(digest.post_digest(conn, None, "tech", now=now, top=2, window_hours=24, due_after=slot,
                                          transport=t)) is None
    assert asyncio.run(digest.post_digest(conn, None, "tech", now=now + timedelta(minutes=1), top=2,
                                          window_hours=24, transport=t)) is None
    [(mid, day)] = conn.execute("SELECT thread_id, day FROM digests").fetchall()

    # 更新: 重要度が変わったら並びが変わって書き換わる（前のまとめがないので window_hours 分）
    with conn:
        conn.execute("UPDATE items SET priority = 0.99 WHERE key = 'a'")
    updated = asyncio.run(digest.post_digest(conn, None, "tech", now=now, top=2, window_hours=24,
                                             update=True, transport=t))
    assert fake.messages[mid]["content"] == updated and "- [小ネタ]" in updated.splitlines()[2]
    assert asyncio.run(digest.post_digest(conn, None, "other", now=now, top=2, window_hours=24,
                                          update=True, transport=t)) is None  # 未投稿は更新しない

    # 次の定時: 前回のまとめ以降に投稿した記事だけ
    later = now + timedelta(hours=12)
    with conn:
        iid = db.insert_item(conn, feed_id, "d", None, "新着", ago(hours=0), "notified")
        db.update_item(conn, iid, notified_at=(now + timedelta(hours=1)).isoformat(timespec="seconds"), discord_thread_id="t-d")
    nxt = asyncio.run(digest.post_digest(conn, None, "tech", now=later, top=2, window_hours=24,
                                         due_after=later - timedelta(minutes=5), transport=t))
    assert "（1 件）" in nxt.splitlines()[0] and "新着" in nxt

    # 削除: メッセージと記録が消える
    assert digest.delete_digest(conn, "tech", day, transport=t)
    assert mid not in fake.messages and not digest.delete_digest(conn, "tech", day, transport=t)

    # 投稿時刻: 直近の times（ローカル時刻）
    def local(*args):
        return datetime(*args).astimezone()

    assert digest.last_slot(local(2026, 11, 8, 7, 0), ["06:30", "18:30"]) == local(2026, 11, 8, 6, 30)
    assert digest.last_slot(local(2026, 11, 8, 5, 0), ["06:30", "18:30"]) == local(2026, 11, 7, 18, 30)
    assert digest.next_slot(local(2026, 11, 8, 20, 0), ["06:30", "18:30"]) == local(2026, 11, 9, 6, 30)

    # 興味のうすいサブジャンル（digest = false）は載せない
    since = datetime.now(UTC) - timedelta(hours=24)
    with conn:
        conn.execute("UPDATE items SET subgenre = 'tech/sports' WHERE key = 'b'")
    assert {i.title for i in digest.collect(conn, "tech", since, {"tech/sports": "low"})} == {"小ネタ", "判定なし", "新着"}
    assert digest.collect(conn, "tech", since, {"tech": "mute"}) == []
    with conn:  # 好きなサブジャンルの記事は、重要度に LIKE_BONUS を足して上に寄せる
        conn.execute("UPDATE items SET subgenre = 'tech/ai', priority = 0.8 WHERE key = 'c'")
    assert next(i.title for i in digest.collect(conn, "tech", since)) == "小ネタ"  # 0.99
    assert next(i.title for i in digest.collect(conn, "tech", since, {"tech/ai": "like"})) == "判定なし"  # 0.8 + 0.3

    many = [digest.DigestItem(f"記事{i}" * 20, f"t{i}", 0.5, None) for i in range(50)]
    text = digest.render(["話題 A", "話題 B"], many, "1", top=50)
    assert len(text) <= digest.MAX_CONTENT and text.endswith("件") and text.startswith("**話題**\n- 話題 A\n- 話題 B\n")
    assert digest.render([], many[:1], "1", top=1).startswith("**注目の記事**")


def test_notify_writes_discord_link(tmp_path):
    fake, client, conn = setup()
    plan = make_plan(client, {"id": "1", "name": "srv"}, feeds_by_category(conn, ["tech"]), "kawarabanya")
    apply_plan(client, conn, plan, "kawarabanya")
    md = tmp_path / "a.md"
    md.write_text('---\ntitle: "x"\n---\n\n# x\n\n本文\n')
    feed_id = db.list_feeds(conn, category="tech")[0].id
    with conn:
        iid = db.insert_item(conn, feed_id, "a", "https://a", "x", ago(hours=1), "saved")
        db.update_item(conn, iid, md_path=str(md))
    fake.rate_limited_once = True
    notify.notify_pending(conn, limit=5, max_age_days=3, transport=httpx.MockTransport(fake.handler), interval=0)
    assert 'discord: "https://discord.com/channels/1/thread1"' in md.read_text()


def test_retag_posts(tmp_path):
    import asyncio

    fake, client, conn = setup()
    genres = tech_genres(tmp_path)
    plan = make_plan(client, {"id": "1", "name": "srv"}, feeds_by_category(conn), "kawarabanya", genres)
    apply_plan(client, conn, plan, "kawarabanya")
    feed_id = db.list_feeds(conn, category="tech")[0].id
    with conn:
        a = db.insert_item(conn, feed_id, "a", None, "判定済み", ago(hours=1), "notified")
        db.update_item(conn, a, discord_thread_id="thread-a", subgenre="tech/security")
        b = db.insert_item(conn, feed_id, "b", None, "未判定・本文なし", ago(hours=1), "notified")
        db.update_item(conn, b, discord_thread_id="thread-b")
    changed, total = asyncio.run(notify.retag_posts(conn, client, None, genres))
    sec = next(t["id"] for c in fake.channels if c["id"] == "11" for t in c["available_tags"] if t["name"] == "セキュリティ")
    assert (changed, total) == (2, 2)
    assert fake.thread_tags == {"thread-a": [sec], "thread-b": []}  # 古いフィード名のタグは外れる


def test_delete_duplicate_posts():
    fake, client, conn = setup()
    feed_id = db.list_feeds(conn)[0].id
    with conn:
        for key, status in [("a", "notified"), ("b", "duplicate"), ("c", "duplicate")]:
            iid = db.insert_item(conn, feed_id, key, None, key, ago(hours=1), status)
            db.update_item(conn, iid, discord_thread_id=f"thread-{key}")
    fake.deleted.append("/channels/thread-c")  # すでに消えている
    assert len(notify.delete_duplicate_posts(conn, client)) == 2 and fake.deleted == ["/channels/thread-c"]
    assert len(notify.delete_duplicate_posts(conn, client, apply=True)) == 2
    assert fake.deleted == ["/channels/thread-c", "/channels/thread-b"]
    assert notify.delete_duplicate_posts(conn, client) == []
    assert conn.execute("SELECT discord_thread_id FROM items WHERE key = 'a'").fetchone()[0] == "thread-a"


def test_pending_items_follow_item_genre(tmp_path):
    _fake, client, conn = setup()
    plan = make_plan(client, {"id": "1", "name": "srv"}, feeds_by_category(conn), "kawarabanya",
                     tech_genres(tmp_path))
    apply_plan(client, conn, plan, "kawarabanya")
    feed_id = db.find_feed_by_url(conn, "https://6tech.example/既存フィード").id
    with conn:
        a = db.insert_item(conn, feed_id, "a", "https://a", "映画の記事", ago(hours=1), "saved")
        db.update_item(conn, a, subgenre="culture/movie")  # tech のフィードでも、LLM が別のジャンルに振り分けた
    assert notify.pending_items(conn, "culture") == ([], 1)  # culture のフォーラムへ（テストでは未作成なので送り先なし）
    assert notify.pending_items(conn, "tech") == ([], 0)

    # ミュートのジャンルの記事は投稿せず muted にする
    with conn:
        m = db.insert_item(conn, feed_id, "m", "https://m", "ミュートの記事", ago(hours=1), "saved")
        db.update_item(conn, m, subgenre="tech/ai")
    stats = notify.notify_pending(conn, limit=5, max_age_days=3, levels={"tech/ai": "mute"}, interval=0)
    assert stats.muted == 1 and stats.sent == 0
    assert conn.execute("SELECT status FROM items WHERE id = ?", (m,)).fetchone()[0] == "muted"


def test_feed_interest_down_and_mute(tmp_path):
    from kawarabanya.rss import digest

    _fake, client, conn = setup()
    a = db.list_feeds(conn, category="tech")[0].id
    b = db.insert_feed(conn, "https://b.example/f", "B", None, "tech")
    apply_plan(client, conn, make_plan(client, {"id": "1", "name": "srv"}, feeds_by_category(conn, ["tech"]), "kawarabanya"),
               "kawarabanya")
    with conn:
        for key, fid, prio in [("x", a, 0.9), ("y", b, 0.7)]:
            iid = db.insert_item(conn, fid, key, None, key, ago(hours=1), "notified")
            db.update_item(conn, iid, notified_at=ago(hours=1), discord_thread_id=f"t-{key}", priority=prio)
    since = datetime.now(UTC) - timedelta(hours=24)
    assert [i.title for i in digest.collect(conn, "tech", since)] == ["x", "y"]
    db.set_option(conn, a, "interest", "down")  # サイトごとの低め: 0.9 - 0.3 < 0.7
    assert [i.title for i in digest.collect(conn, "tech", since)] == ["y", "x"]
    db.set_option(conn, a, "interest", "low")
    assert [i.title for i in digest.collect(conn, "tech", since, {"tech": "like"})] == ["y"]  # サイトの設定が優先

    with conn:
        db.insert_item(conn, a, "m", "https://m", "ミュート", ago(hours=1), "saved")
    db.set_option(conn, a, "interest", "mute")
    assert notify.notify_pending(conn, limit=5, max_age_days=3, interval=0).muted == 1


def test_health_alerts_once_and_rearm():
    from kawarabanya.rss import health

    fake, client, conn = setup()
    t = httpx.MockTransport(fake.handler)
    feed_id = db.list_feeds(conn)[0].id
    with conn:
        db.set_enabled(conn, feed_id, False, "fail_x5:forbidden")
        conn.execute("UPDATE feeds SET last_error = 'HTTP 403' WHERE id = ?", (feed_id,))
    found = health.find_problems(conn)
    assert list(found) == [f"feed:{feed_id}"] and "HTTP 403" in found[f"feed:{feed_id}"]
    assert health.send_alerts(conn, found, t) == []  # 送り先がまだない → 覚えず、次に送る

    fake.channels.append({"id": "50", "type": 0, "name": "ニュース", "parent_id": "999"})
    discord_api.register_webhook(client, conn, discord_api.DIGEST_KEY, "1", "50", "ニュース", "kawarabanya")
    assert health.send_alerts(conn, found, t) == [f"feed:{feed_id}"]
    assert "取得を止めました" in fake.posts[-1]["content"] and fake.posts[-1]["allowed_mentions"] == {"parse": []}
    count = len(fake.posts)
    assert health.send_alerts(conn, found, t) == []  # 同じ不調は送らない
    assert len(fake.posts) == count

    assert health.send_alerts(conn, {}, t) == []  # 直ったら忘れる
    assert health.send_alerts(conn, found, t) == [f"feed:{feed_id}"]  # 再発したらまた送る
