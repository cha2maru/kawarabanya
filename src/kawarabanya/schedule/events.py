"""Google カレンダーの予定を読み（gcsa・読み取り専用の OAuth）、DB と比べて Discord に知らせる。

- 朝の予定（[schedule] agenda_times）: 今から明日の終わりまでの予定の一覧。
- 直前の通知: 開始の remind_minutes 分前（終日の予定は朝の一覧に任せる）。
- 変更の通知: 前回からの追加・変更・取り消し。初回は記録するだけ。

Discord には時刻・件名・場所だけを送り、説明文は送らない。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from kawarabanya.core import db
from kawarabanya.core.config import ScheduleConfig

SCOPE_NOTE = "読み取り専用（https://www.googleapis.com/auth/calendar.readonly）"
WEEKDAYS = "月火水木金土日"


@dataclass
class Ev:
    """予定 1 件（繰り返しの予定は 1 回ずつ）。start・end は ISO 形式（終日なら日付だけ）。"""
    id: str
    calendar: str
    summary: str
    start: str
    end: str
    all_day: bool
    location: str = ""
    updated: str = ""

    def start_at(self) -> datetime:
        """開始のローカル日時（終日なら 0 時）。"""
        if self.all_day:
            return datetime.combine(date.fromisoformat(self.start), time()).astimezone()
        return datetime.fromisoformat(self.start).astimezone()


def login(cfg: ScheduleConfig, open_browser: bool | None = None):
    """gcsa の GoogleCalendar（読み取り専用）。トークンがなければブラウザで許可を求めて保存する。"""
    from gcsa.google_calendar import GoogleCalendar

    if not Path(cfg.credentials).exists():
        raise ValueError(f"{cfg.credentials} がありません（Google Cloud の OAuth クライアント ID の JSON。README の「予定」）")
    Path(cfg.token).parent.mkdir(parents=True, exist_ok=True)
    return GoogleCalendar(
        credentials_path=cfg.credentials, token_path=cfg.token, read_only=True,
        # 既定の 8080 は要約サーバーと重なる
        authentication_flow_port=cfg.login_port, open_browser=open_browser,
    )


def fetch(gc, cfg: ScheduleConfig, now: datetime) -> list[Ev]:
    """今から days 日分の予定（繰り返しは 1 回ずつに展開したもの）。calendars が空ならすべてのカレンダー。"""
    ids = cfg.calendars or [c.id for c in gc.get_calendar_list()]
    out: list[Ev] = []
    for cal in ids:
        for e in gc.get_events(time_min=now, time_max=now + timedelta(days=cfg.days), single_events=True,
                               order_by="startTime", calendar_id=cal):
            all_day = not isinstance(e.start, datetime)
            out.append(Ev(
                id=f"{cal}:{e.id}", calendar=cal, summary=e.summary or "(無題)", start=e.start.isoformat(),
                end=(e.end or e.start).isoformat(), all_day=all_day, location=e.location or "",
                updated=e.updated.isoformat() if e.updated else "",
            ))
    return out


def _row(r: sqlite3.Row) -> Ev:
    return Ev(r["id"], r["calendar"], r["summary"], r["start"], r["end"], bool(r["all_day"]), r["location"] or "",
              r["updated"] or "")


def sync(conn: sqlite3.Connection, events: list[Ev], now: datetime) -> list[tuple[str, Ev]]:
    """取った予定を DB と比べて保存し、(追加 / 変更 / 取り消し, 予定) の一覧を返す。初回は保存するだけ。

    取る範囲は日ごとに先へずれるので、範囲に入ってきただけの予定は追加と数えない（前回より後に更新されたものだけ）。
    """
    last = db.get_state(conn, "schedule_synced_at")
    stored = {r["id"]: _row(r) for r in conn.execute("SELECT * FROM events")}
    changes: list[tuple[str, Ev]] = []
    with conn:
        for e in events:
            old = stored.pop(e.id, None)
            if old is None:
                if last and e.updated and datetime.fromisoformat(e.updated) > datetime.fromisoformat(last):
                    changes.append(("追加", e))
            elif (old.summary, old.start, old.end, old.location) != (e.summary, e.start, e.end, e.location):
                changes.append(("変更", e))
            conn.execute(
                "INSERT INTO events (id, calendar, summary, start, end, all_day, location, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET summary = excluded.summary,"
                " start = excluded.start, end = excluded.end, all_day = excluded.all_day,"
                " location = excluded.location, updated = excluded.updated,"
                # 時刻が変わったら、直前の通知をやり直す
                " reminded_at = CASE WHEN events.start = excluded.start THEN events.reminded_at END",
                (e.id, e.calendar, e.summary, e.start, e.end, int(e.all_day), e.location, e.updated),
            )
        for old in stored.values():
            conn.execute("DELETE FROM events WHERE id = ?", (old.id,))
            # 始まった・終わった予定は範囲から外れただけ
            if old.start_at() > now:
                changes.append(("取り消し", old))
        db.set_state(conn, "schedule_synced_at", now.isoformat(timespec="seconds"))
    return [] if last is None else changes


def due_reminders(conn: sqlite3.Connection, now: datetime, minutes: int) -> list[Ev]:
    """開始の minutes 分前を過ぎ、まだ始まっていない予定（終日を除く）を返し、通知済みにする。"""
    rows = [_row(r) for r in conn.execute("SELECT * FROM events WHERE all_day = 0 AND reminded_at IS NULL")]
    due = [e for e in rows if now < e.start_at() <= now + timedelta(minutes=minutes)]
    with conn:
        for e in due:
            conn.execute("UPDATE events SET reminded_at = ? WHERE id = ?", (db.now_iso(), e.id))
    return sorted(due, key=Ev.start_at)


def agenda(conn: sqlite3.Connection, now: datetime) -> list[Ev]:
    """今から明日の終わりまでの予定（終わっていないもの）。"""
    until = datetime.combine(now.astimezone().date() + timedelta(days=2), time()).astimezone()
    rows = [_row(r) for r in conn.execute("SELECT * FROM events")]
    return sorted((e for e in rows if e.start_at() < until and _end_at(e) > now), key=Ev.start_at)


def _end_at(e: Ev) -> datetime:
    if e.all_day:
        return datetime.combine(date.fromisoformat(e.end), time()).astimezone()
    return datetime.fromisoformat(e.end).astimezone()


def line(e: Ev) -> str:
    """予定 1 行（「11/9(日) 10:00–11:00 歯医者 📍駅前」）。"""
    s = e.start_at()
    day = f"{s.month}/{s.day}({WEEKDAYS[s.weekday()]})"
    when = "終日" if e.all_day else f"{s:%H:%M}–{_end_at(e):%H:%M}"
    return f"{day} {when} {e.summary}" + (f" 📍{e.location}" if e.location else "")
