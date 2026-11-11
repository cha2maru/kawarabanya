"""Slack のワークスペースの準備のコマンド（slack setup）。"""

from __future__ import annotations

import argparse
import sqlite3

from kawarabanya.cli.common import _load_genres, _open_db
from kawarabanya.core import chat as chatdb
from kawarabanya.core import slack_api
from kawarabanya.core.chat import DIGEST_KEY, SCHEDULE_KEY, Channel
from kawarabanya.core.config import Config
from kawarabanya.rss import forums
from kawarabanya.rss.genres import Genre


def _client(cfg: Config) -> slack_api.SlackClient:
    if not cfg.slack.token:
        raise ValueError(f"Slack の Bot のトークンがありません（.env の {cfg.slack.token_env}=xoxb-...。README の「Slack」）")
    return slack_api.SlackClient(cfg.slack.token)


def plan_channels(cfg: Config, conn: sqlite3.Connection, defs: dict[str, Genre] | None) -> list[tuple[str, str, bool, dict]]:
    """用意する送り先: (キー, チャンネル名, 非公開か, タグ)。ジャンルごと・まとめ・予定。"""
    if defs:
        tops = [g.path for g in defs.values() if not g.is_sub]
    else:
        tops = list(forums.feeds_by_category(conn))
    out = [
        (top, slack_api.channel_name(cfg.slack.channel_prefix, top), False,
         {f"sub:{g.path}": g.label for g in (defs or {}).values() if g.is_sub and g.top == top})
        for top in tops
    ]
    if cfg.digest.enabled:
        out.append((DIGEST_KEY, slack_api.channel_name(cfg.slack.channel_prefix, "digest"), False, {}))
    if cfg.schedule.enabled:
        # 予定はほかの人に見せないので非公開にする
        out.append((SCHEDULE_KEY, slack_api.channel_name(cfg.slack.channel_prefix, "schedule"), True, {}))
    return out


def setup_channels(cfg: Config, conn: sqlite3.Connection, defs: dict[str, Genre] | None, *, apply: bool) -> list[str]:
    """ジャンル・まとめ・予定の Slack のチャンネルを（なければ作って）登録する。予定の説明の行を返す。"""
    client = _client(cfg)
    team = client.me()["team_id"]
    existing = {c["name"]: c for c in client.channels()}
    lines = []
    for key, name, private, tags in plan_channels(cfg, conn, defs):
        found = existing.get(name)
        lines.append(f"  {'既存' if found else '作成'}  #{name} ← {key}" + ("（非公開）" if private else ""))
        if not apply:
            continue
        if found is None:
            found = client.call("conversations.create", name=name, is_private=private)["channel"]
            if private and cfg.slack.mention_user_id:
                client.call("conversations.invite", channel=found["id"], users=cfg.slack.mention_user_id)
        elif not found.get("is_member"):
            client.call("conversations.join", channel=found["id"])  # 公開チャンネルだけ。非公開は Bot を招待してもらう
        chatdb.save_channel(conn, Channel("slack", key, found["id"], name, None, team, tags))
    return lines


def cmd_slack_setup(cfg: Config, args: argparse.Namespace) -> int:
    """slack setup: ジャンルごとのチャンネルと、まとめ・予定のチャンネルを揃える（--apply がなければ予定を表示）。"""
    conn = _open_db(cfg, args)
    for line in setup_channels(cfg, conn, _load_genres(cfg), apply=args.apply):
        print(line)
    if not args.apply:
        print("\n--apply を付けると作成して登録します（[chat] targets に \"slack\" を足すと投稿が始まる）")
        return 0
    print("\n登録しました")
    return 0
