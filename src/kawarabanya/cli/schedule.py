"""予定の機能のコマンド（Google カレンダーの許可・予定の確認と通知）。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from kawarabanya.cli.common import _open_db
from kawarabanya.core import db, discord_api
from kawarabanya.core.config import Config
from kawarabanya.rss.digest import last_slot
from kawarabanya.schedule import events


def _calendar(cfg: Config, *, interactive: bool):
    """許可済みの GoogleCalendar。interactive=False でトークンがなければ ValueError（Bot から許可の画面は出せない）。"""
    if not interactive and not Path(cfg.schedule.token).exists():
        raise ValueError("Google カレンダーをまだ許可していません（kawarabanya schedule login）")
    return events.login(cfg.schedule, open_browser=None if interactive else False)


def cmd_schedule_login(cfg: Config, args: argparse.Namespace) -> int:
    """schedule login: ブラウザで Google カレンダーの読み取りを許可し、トークンを保存する。"""
    print(f"ブラウザで Google アカウントを選び、{events.SCOPE_NOTE}を許可してください"
          f"（開かなければ表示される URL を開く。戻り先は localhost:{cfg.schedule.login_port}）", flush=True)
    gc = _calendar(cfg, interactive=True)
    names = [f"{c.summary}（{c.id}）" for c in gc.get_calendar_list()]
    print(f"許可しました: {cfg.schedule.token}\nカレンダー: " + "、".join(names))
    return 0


def _post(conn, content: str, user_id: str = "") -> bool:
    row = conn.execute("SELECT webhook_url FROM discord_channels WHERE category = ?", (discord_api.SCHEDULE_KEY,)).fetchone()
    if row is None:
        return False
    payload = {"content": content[:2000], "username": "予定", "allowed_mentions": {"parse": []}}
    discord_api.send_webhook(row["webhook_url"], discord_api.with_mention(payload, user_id))
    return True


def cmd_schedule_check(cfg: Config, args: argparse.Namespace) -> int:
    """schedule check: 予定を読み、変更・直前の予定・（時刻なら）今日と明日の予定を Discord に知らせる。"""
    conn = _open_db(cfg, args)
    now = datetime.now().astimezone()
    from google.auth.exceptions import GoogleAuthError
    from googleapiclient.errors import HttpError
    from httplib2 import HttpLib2Error

    gc = _calendar(cfg, interactive=False)
    try:
        fetched = events.fetch(gc, cfg.schedule, now)
    except (GoogleAuthError, HttpError, HttpLib2Error, OSError) as e:  # 許可の失効・通信の失敗など
        print(f"Google カレンダーを読めませんでした: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    messages = []
    if changes := events.sync(conn, fetched, now):
        messages.append("**予定の変更**\n" + "\n".join(f"- {kind}: {events.line(e)}" for kind, e in changes))
    messages += [f"⏰ まもなく: {events.line(e)}" for e in events.due_reminders(conn, now, cfg.schedule.remind_minutes)]
    slot = last_slot(now, cfg.schedule.agenda_times)
    last = db.get_state(conn, "schedule_agenda_at")
    if args.agenda or (slot and (last is None or datetime.fromisoformat(last) < slot)):
        items = events.agenda(conn, now)
        messages.append("**今日と明日の予定**\n" + ("\n".join(f"- {events.line(e)}" for e in items) or "予定はありません"))
        if not args.dry_run:
            db.set_state(conn, "schedule_agenda_at", now.isoformat(timespec="seconds"))
    for m in messages:
        if args.dry_run or not _post(conn, m, cfg.discord.mention_user_id):
            print(m)
    print(f"予定 {len(fetched)} 件（{cfg.schedule.days} 日先まで）/ 知らせたもの {len(messages)} 件")
    return 0
