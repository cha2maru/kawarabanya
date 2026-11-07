"""doctor: 動かすのに必要なものがそろっているかを調べる（インストール後・不調のとき用）。"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx

from kawarabanya.core import db
from kawarabanya.core.config import Config
from kawarabanya.core.discord_api import DIGEST_KEY, DiscordClient, DiscordError
from kawarabanya.rss import genres, health
from kawarabanya.rss.sources import extensions

# GET /applications/@me の flags（MESSAGE CONTENT INTENT がオンか）
GATEWAY_MESSAGE_CONTENT = (1 << 18) | (1 << 19)
MARKS = {"ok": "✅", "warn": "⚠️ ", "ng": "❌"}


class Report:
    """調べた結果を 1 行ずつ表示し、❌ の数を数える。"""

    def __init__(self) -> None:
        self.failures = 0

    def __call__(self, level: str, what: str, detail: str = "") -> None:
        self.failures += level == "ng"
        print(f"{MARKS[level]} {what}" + (f": {detail}" if detail else ""))


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    """doctor: Python・設定ファイル・DB・Discord・要約サーバー・常駐サービスを順に調べる。❌ があれば 1 を返す。"""
    r = Report()
    r("ok" if sys.version_info >= (3, 12) else "ng", "Python", sys.version.split()[0] + "（3.12 以上が必要）")

    # 設定ファイル
    if cfg.path:
        r("ok", "config.toml", str(cfg.path))
    else:
        r("warn", "config.toml", "ありません（すべて既定値で動きます。見本: config.example.toml）")
    defs = None
    gpath = Path(cfg.general.genres_file)
    if not gpath.exists():
        r("ng", "genres.toml", f"{gpath} がありません（見本: genres.example.toml）")
    else:
        try:
            defs = genres.load_genres(gpath)
            tops = [g for g in defs.values() if not g.is_sub]
            r("ok", "genres.toml", f"ジャンル {len(tops)} 個・サブジャンル {len(defs) - len(tops)} 個")
        except (ValueError, OSError) as e:
            r("ng", "genres.toml", str(e))
    r("ok", "拡張", "、".join(extensions.available()))

    # DB と保存先
    db_path = Path(args.db or cfg.general.db)
    try:
        conn = db.connect(db_path)
        feeds = db.list_feeds(conn, enabled=True)
        r("ok", "DB", f"{db_path}（版 {db.schema_version(conn)}・有効なフィード {len(feeds)} 件）")
        if not feeds:
            r("warn", "フィード", "ありません（import-opml か feeds add で登録する）")
        channels = conn.execute("SELECT category FROM discord_channels").fetchall()
    except (OSError, ValueError, sqlite3.Error) as e:
        r("ng", "DB", f"{db_path}: {e}")
        channels = []
    archive = Path(cfg.general.archive_dir)
    try:
        archive.mkdir(parents=True, exist_ok=True)
        probe = archive / ".doctor"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        r("ok", "保存先", str(archive))
    except OSError as e:
        r("ng", "保存先", f"{archive} に書き込めません: {e}")

    _check_rsshub(cfg, r)
    _check_discord(cfg, r, channels)
    _check_llm(cfg, r)
    _check_service(r)
    print("問題はありません" if r.failures == 0 else f"❌ が {r.failures} 件あります")
    return 1 if r.failures else 0


def _check_rsshub(cfg: Config, r: Report) -> None:
    base = cfg.general.rsshub_url.rstrip("/")
    if not base:
        return
    ok = health.rsshub_alive(base)
    # 止まっていると RSSHub 経由のフィードが取得に失敗し続け、5 回で自動的に無効になる
    r("ok" if ok else "ng", "RSSHub", base if ok else f"{base} に応答がありません（docker start rsshub。Docker Desktop が起動しているか）")


def _check_discord(cfg: Config, r: Report, channels: list) -> None:
    token = cfg.discord.token
    if not token:
        r("ng", "Discord", f"Bot のトークンがありません（.env に {cfg.discord.token_env}=... を書く。見本: .env.example）")
        return
    client = DiscordClient(token)
    try:
        me = client.request("GET", "/users/@me")
        app = client.request("GET", "/applications/@me")
        guilds = client.guilds()
    except (DiscordError, httpx.HTTPError) as e:
        r("ng", "Discord", f"Bot のトークンで接続できません: {e}")
        return
    r("ok", "Discord の Bot", f"{me.get('username')}")
    if app.get("flags", 0) & GATEWAY_MESSAGE_CONTENT:
        r("ok", "MESSAGE CONTENT INTENT", "オン（Discord から日本語で頼める）")
    else:
        r("warn", "MESSAGE CONTENT INTENT", "オフ（Developer Portal の Bot の設定でオンにすると、Discord から日本語で頼める）")
    if not guilds:
        r("ng", "Discord のサーバー", "Bot がどのサーバーにも参加していません（kawarabanya discord invite-url の URL から招待する）")
    elif len(guilds) > 1 and not cfg.discord.guild_id:
        r("ng", "Discord のサーバー", f"{len(guilds)} 個に参加しています（config.toml の [discord] guild_id で 1 つ選ぶ）")
    else:
        r("ok", "Discord のサーバー", "、".join(g["name"] for g in guilds))
    forums = [c[0] for c in channels if c[0] != DIGEST_KEY]
    if forums:
        r("ok", "フォーラム", f"{len(forums)} 個（まとめの投稿先: {'あり' if len(forums) < len(channels) else 'なし'}）")
    else:
        r("warn", "フォーラム", "まだありません（kawarabanya discord setup --apply で作る）")


def _check_llm(cfg: Config, r: Report) -> None:
    if not cfg.summarize.enabled:
        r("warn", "要約サーバー", "使わない設定です（[summarize] enabled = false）")
        return
    cmd = cfg.bot.llm_server_command
    if cmd:
        exe = shlex.split(cmd)[0]
        found = Path(exe).exists() or shutil.which(exe)
        r("ok" if found else "ng", "要約サーバーの起動コマンド", cmd if found else f"{exe} がありません")
    base = cfg.summarize.endpoint.rstrip("/")
    try:
        models = httpx.get(f"{base}/models", timeout=5).json().get("data", [])
        names = [m.get("id") for m in models]
        r("ok", "要約サーバー", f"{base}（モデル: {', '.join(names) or '?'}）")
    except (httpx.HTTPError, ValueError):
        how = "Bot を起動すると一緒に起動します" if cmd else "scripts/llama-server.sh などで起動する"
        r("warn", "要約サーバー", f"{base} に応答がありません（{how}。なくても取り込み・投稿はできる）")


def _check_service(r: Report) -> None:
    if not shutil.which("systemctl") or not os.environ.get("XDG_RUNTIME_DIR"):
        return
    state = subprocess.run(
        ["systemctl", "--user", "is-active", "kawarabanya-bot"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if state == "active":
        r("ok", "常駐サービス", "kawarabanya-bot が動いています")
    else:
        r("warn", "常駐サービス", f"kawarabanya-bot は {state or '未登録'}（scripts/install.sh --service で登録する）")
