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
