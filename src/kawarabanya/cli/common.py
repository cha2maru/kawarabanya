"""CLI の共通部品（表示の補助・DB や Discord クライアントの用意）。"""

from __future__ import annotations

import argparse
import sqlite3
import unicodedata
from pathlib import Path

from kawarabanya.core import db, discord_api
from kawarabanya.core.config import Config
from kawarabanya.rss import genres
from kawarabanya.rss.sources.check import Status

STATUS_MARK = {
    Status.OK: "OK  ",
    Status.NOT_FEED: "NOTF",
    Status.GONE: "GONE",
    Status.FORBIDDEN: "DENY",
    Status.HTTP_ERROR: "HTTP",
    Status.DNS: "DNS ",
    Status.NETWORK: "NET ",
}

def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)

def _fit(s: str, width: int) -> str:
    """全角文字を 2 桁として、指定幅に切り詰め・空白埋めする。"""
    out, w = "", 0
    for c in s:
        cw = _width(c)
        if w + cw > width:
            if out:
                out = out[:-1] if _width(out[-1]) == 1 else out[:-1] + " "
                out += "…"
            break
        out += c
        w += cw
    return out + " " * (width - _width(out))

def _open_db(cfg: Config, args: argparse.Namespace) -> sqlite3.Connection:
    """設定（--db があればそちら）の DB を開く。"""
    return db.connect(args.db or cfg.general.db)

TOKEN_HELP = """Bot のトークンが設定されていません。
  1. https://discord.com/developers/applications で New Application → Bot → Reset Token
  2. .env に DISCORD_BOT_TOKEN=<トークン> を書く
  3. kawarabanya discord invite-url で表示される URL から、Bot をサーバーに招待する"""

def _discord_client(cfg: Config) -> discord_api.DiscordClient:
    """Bot トークンで Discord の REST クライアントを作る。トークンがなければ手順付きの ValueError。"""
    token = cfg.discord.token
    if not token:
        raise ValueError(TOKEN_HELP)
    return discord_api.DiscordClient(token)

def _load_genres(cfg: Config) -> dict[str, genres.Genre] | None:
    """genres.toml を読む。ファイルがなければ None。"""
    path = Path(cfg.general.genres_file)
    return genres.load_genres(path) if path.exists() else None

def _require_genres(cfg: Config) -> dict[str, genres.Genre]:
    """genres.toml を読む。ファイルがなければ ValueError。"""
    defs = _load_genres(cfg)
    if defs is None:
        raise ValueError(f"{cfg.general.genres_file} がありません（genres.example.toml を参考に作成してください）")
    return defs
