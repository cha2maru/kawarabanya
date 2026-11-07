"""不調を Discord に知らせる（まとめの投稿先のチャンネルへ）。

Bot が定期実行のたびに調べ、新しく起きた不調だけを 1 通にまとめて送る。送ったものは state の alerted に
覚えておき、直ると忘れる（同じ不調を毎時送らず、再発したらまた送る）。
"""

from __future__ import annotations

import json
import logging
import sqlite3

import httpx

from kawarabanya.core import db
from kawarabanya.core.discord_api import DiscordError, send_webhook
from kawarabanya.rss.digest import digest_channel

log = logging.getLogger(__name__)

MAX_CONTENT = 1900


def rsshub_alive(url: str) -> bool:
    """RSSHub が応答するか。"""
    try:
        return httpx.get(url.rstrip("/") + "/", timeout=10).is_success
    except httpx.HTTPError:
        return False


def find_problems(conn: sqlite3.Connection, rsshub_url: str = "") -> dict[str, str]:
    """今の不調（キー → 説明）。失敗が続いて自動で止まったフィードと、RSSHub の無応答。"""
    found = {
        f"feed:{r['id']}": f"「{r['title'] or r['url']}」の取得を止めました（{r['disabled_reason']}）。{r['last_error'] or ''}"
        for r in conn.execute(
            "SELECT id, title, url, disabled_reason, last_error FROM feeds"
            " WHERE enabled = 0 AND disabled_reason LIKE 'fail_x%'"
        )
    }
    if rsshub_url and not rsshub_alive(rsshub_url):
        found["rsshub"] = f"RSSHub（{rsshub_url}）が応答しません。止まったままだと、経由のフィードも 5 回で止まります"
    return found


def send_alerts(
    conn: sqlite3.Connection, found: dict[str, str], transport: httpx.BaseTransport | None = None
) -> list[str]:
    """found のうち、まだ知らせていないものを送り、送ったキーを返す。送れなければ次の回に送り直す。"""
    known = set(json.loads(db.get_state(conn, "alerted") or "[]"))
    new = [k for k in found if k not in known]
    channel = digest_channel(conn)
    if new and channel:
        lines = [f"- {found[k]}" for k in new]
        content = "⚠️ **kawarabanya の不調**\n" + "\n".join(lines)
        if len(content) > MAX_CONTENT:
            content = content[:MAX_CONTENT] + "\n…（ほかは kawarabanya status で確かめてください）"
        try:
            send_webhook(
                channel["webhook_url"],
                {"username": "瓦版屋 お知らせ", "content": content, "allowed_mentions": {"parse": []}},
                transport=transport,
            )
        except (DiscordError, httpx.HTTPError) as e:
            log.warning("不調の通知に失敗: %s", e)
            new = []
    elif new:
        new = []  # 送り先がまだない
    # 直ったものは忘れる。送れなかったものは覚えない
    db.set_state(conn, "alerted", json.dumps(sorted((known & set(found)) | set(new))))
    return new
