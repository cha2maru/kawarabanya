from datetime import date, datetime, timedelta

from kawarabanya.core import db
from kawarabanya.core.config import ScheduleConfig
from kawarabanya.schedule import events

NOW = datetime(2026, 11, 9, 9, 0).astimezone()


def ev(i, start, minutes=60, summary="会議", updated=NOW - timedelta(days=1), **kw):
    if isinstance(start, date) and not isinstance(start, datetime):
        return events.Ev(str(i), "c", summary, start.isoformat(), (start + timedelta(days=1)).isoformat(), True,
                         updated=updated.isoformat(), **kw)
    return events.Ev(str(i), "c", summary, start.isoformat(), (start + timedelta(minutes=minutes)).isoformat(), False,
                     updated=updated.isoformat(), **kw)


def test_sync_reports_changes_after_first_time():
    conn = db.connect(":memory:")
    a, b = ev(1, NOW + timedelta(hours=3)), ev(2, NOW + timedelta(days=1))
    assert events.sync(conn, [a, b], NOW) == []  # 初回は記録だけ
    later = NOW + timedelta(minutes=5)
    moved = ev(1, NOW + timedelta(hours=4))
    new = ev(3, NOW + timedelta(days=2), summary="歯医者", updated=later)
    entered = ev(4, NOW + timedelta(days=14))  # 範囲に入ってきただけ（前回より前に更新）
    changes = events.sync(conn, [moved, new, entered], later)
    assert [(k, e.id) for k, e in changes] == [("変更", "1"), ("追加", "3"), ("取り消し", "2")]


def test_reminders_once_and_again_after_move():
    conn = db.connect(":memory:")
    e = ev(1, NOW + timedelta(minutes=20))
    events.sync(conn, [e, ev(2, date(2026, 11, 9))], NOW)
    assert [x.id for x in events.due_reminders(conn, NOW, 30)] == ["1"]  # 終日は直前に知らせない
    assert events.due_reminders(conn, NOW, 30) == []
    events.sync(conn, [ev(1, NOW + timedelta(minutes=25)), ev(2, date(2026, 11, 9))], NOW)
    assert [x.id for x in events.due_reminders(conn, NOW, 30)] == ["1"]  # 時刻が変わったら、もう一度


def test_agenda_and_line():
    conn = db.connect(":memory:")
    items = [ev(1, NOW + timedelta(hours=1), summary="歯医者", location="駅前"), ev(2, date(2026, 11, 10)),
             ev(3, NOW + timedelta(days=3)), ev(4, NOW - timedelta(hours=2))]
    events.sync(conn, items, NOW)
    got = events.agenda(conn, NOW)
    assert [e.id for e in got] == ["1", "2"]
    assert events.line(got[0]) == "11/9(月) 10:00–11:00 歯医者 📍駅前"
    assert events.line(got[1]) == "11/10(火) 終日 会議"


def test_fetch_uses_gcsa_events():
    from gcsa.event import Event

    class GC:
        def get_calendar_list(self):
            return [type("Cal", (), {"id": "primary"})()]

        def get_events(self, **kw):
            assert kw["single_events"] and kw["calendar_id"] == "primary"
            return [Event("会議", start=NOW, end=NOW + timedelta(hours=1), event_id="x"),
                    Event("休み", start=date(2026, 11, 10), event_id="y")]

    got = events.fetch(GC(), ScheduleConfig(), NOW)
    assert [(e.id, e.all_day) for e in got] == [("primary:x", False), ("primary:y", True)]


def test_private_channel_hides_from_everyone():
    from kawarabanya.core import discord_api as d

    class Client:
        def request(self, method, path, json=None):
            return {"id": "bot"}

        def create_channel(self, guild_id, name, type_, **extra):
            return {"name": name, "type": type_, **extra}

    ch = d.create_private_text_channel(Client(), "g", "予定")
    everyone, bot = ch["permission_overwrites"]
    assert everyone["id"] == "g" and int(everyone["deny"]) & d.PERM_VIEW_CHANNEL
    assert bot["id"] == "bot" and int(bot["allow"]) & d.PERM_MANAGE_WEBHOOKS


def test_mentions(monkeypatch):
    from kawarabanya.cli import schedule as cli
    from kawarabanya.core import discord_api as d
    from kawarabanya.rss import digest

    assert d.with_mention({"content": "x"}, "") == {"content": "x"}
    m = d.with_mention({"content": "x", "allowed_mentions": {"parse": []}}, "42")
    assert m["content"] == "<@42> x" and m["allowed_mentions"] == {"parse": [], "users": ["42"]}

    sent = []
    monkeypatch.setattr(d, "send_webhook", lambda url, payload, **kw: sent.append(payload))
    monkeypatch.setattr(digest, "send_webhook", lambda url, payload, **kw: sent.append(payload))
    conn = db.connect(":memory:")
    for key in (d.SCHEDULE_KEY, d.DIGEST_KEY):
        conn.execute("INSERT INTO discord_channels (category, guild_id, channel_id, channel_name, webhook_url, created_at)"
                     " VALUES (?, 'g', 'c', 'n', 'https://hook', 'x')", (key,))
    assert cli._post(conn, "⏰ まもなく: 会議", "42")
    digest.announce(conn, ["ニュース", "テック"], "42")
    digest.announce(conn, ["ニュース"], "")  # 設定がなければ送らない
    assert [p["content"] for p in sent] == ["<@42> ⏰ まもなく: 会議", "<@42> 📰 まとめを投稿しました: ニュース、テック"]
