"""Discord の Bot API を使ったチャンネル・Webhook の準備。"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass

import httpx

from . import db

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
GUILD_TEXT = 0
GUILD_CATEGORY = 4

# チャンネル表示・メッセージ送信・チャンネル管理・Webhook 管理
PERM_VIEW_CHANNEL = 1 << 10
PERM_SEND_MESSAGES = 1 << 11
PERM_MANAGE_CHANNELS = 1 << 4
PERM_MANAGE_WEBHOOKS = 1 << 29
BOT_PERMISSIONS = PERM_VIEW_CHANNEL | PERM_SEND_MESSAGES | PERM_MANAGE_CHANNELS | PERM_MANAGE_WEBHOOKS

UNCATEGORIZED = "uncategorized"


class DiscordError(Exception):
    pass


class DiscordClient:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=API,
            transport=transport,
            timeout=20,
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "DiscordBot (https://github.com/, 0.1) feedpipe",
            },
        )

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, json: object = None) -> object:
        for _ in range(5):
            resp = self._client.request(method, path, json=json)
            if resp.status_code == 429:
                wait = float(resp.json().get("retry_after", 1))
                log.info("レート制限のため %.1f 秒待ちます", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("message", resp.text)
                except ValueError:
                    detail = resp.text
                raise DiscordError(f"{method} {path}: HTTP {resp.status_code} {detail}")
            return resp.json() if resp.content else None
        raise DiscordError(f"{method} {path}: レート制限が解除されません")

    def application_id(self) -> str:
        return self.request("GET", "/oauth2/applications/@me")["id"]

    def guilds(self) -> list[dict]:
        return self.request("GET", "/users/@me/guilds")

    def channels(self, guild_id: str) -> list[dict]:
        return self.request("GET", f"/guilds/{guild_id}/channels")

    def create_channel(self, guild_id: str, name: str, type_: int, **extra: object) -> dict:
        return self.request("POST", f"/guilds/{guild_id}/channels", {"name": name, "type": type_, **extra})

    def webhooks(self, channel_id: str) -> list[dict]:
        return self.request("GET", f"/channels/{channel_id}/webhooks")

    def create_webhook(self, channel_id: str, name: str) -> dict:
        return self.request("POST", f"/channels/{channel_id}/webhooks", {"name": name})


def invite_url(application_id: str) -> str:
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&scope=bot&permissions={BOT_PERMISSIONS}"
    )


def resolve_guild(client: DiscordClient, guild_id: str | None) -> dict:
    guilds = client.guilds()
    if guild_id:
        for g in guilds:
            if g["id"] == guild_id:
                return g
        raise DiscordError(f"Bot はサーバー {guild_id} に参加していません")
    if len(guilds) == 1:
        return guilds[0]
    if not guilds:
        raise DiscordError("Bot がどのサーバーにも参加していません（招待 URL から追加してください）")
    names = ", ".join(f"{g['name']} ({g['id']})" for g in guilds)
    raise DiscordError(f"Bot が複数のサーバーに参加しています。config.toml の [discord] guild_id で指定してください: {names}")


def channel_name(category: str | None) -> str:
    """feedpipe のカテゴリを Discord のチャンネル名にする（Discord 側の正規化に合わせる）。"""
    name = (category or UNCATEGORIZED).lower()
    name = re.sub(r"[\s/]+", "-", name)
    name = re.sub(r"[^\w\-ー]", "", name).strip("-")
    return name[:100] or UNCATEGORIZED


@dataclass
class PlannedChannel:
    category: str
    name: str
    channel_id: str | None  # 既存のチャンネルがあればその ID

    @property
    def exists(self) -> bool:
        return self.channel_id is not None


@dataclass
class Plan:
    guild: dict
    parent_name: str
    parent_id: str | None  # None なら親カテゴリを新しく作る
    channels: list[PlannedChannel]


def make_plan(client: DiscordClient, guild: dict, categories: list[str], parent_name: str) -> Plan:
    existing = client.channels(guild["id"])
    parent = next(
        (c for c in existing if c["type"] == GUILD_CATEGORY and c["name"].lower() == parent_name.lower()),
        None,
    )
    by_name = {
        c["name"]: c["id"]
        for c in existing
        if c["type"] == GUILD_TEXT and parent and c.get("parent_id") == parent["id"]
    }
    planned = [
        PlannedChannel(category=cat, name=(n := channel_name(cat)), channel_id=by_name.get(n))
        for cat in categories
    ]
    return Plan(guild=guild, parent_name=parent_name, parent_id=parent["id"] if parent else None, channels=planned)


def apply_plan(client: DiscordClient, conn: sqlite3.Connection, plan: Plan, webhook_name: str) -> list[PlannedChannel]:
    """不足しているチャンネルと Webhook を作り、カテゴリとの対応を DB に保存する。作成したものを返す。"""
    guild_id = plan.guild["id"]
    if plan.parent_id is None:
        plan.parent_id = client.create_channel(guild_id, plan.parent_name, GUILD_CATEGORY)["id"]

    created = []
    for pc in plan.channels:
        if not pc.exists:
            ch = client.create_channel(
                guild_id, pc.name, GUILD_TEXT, parent_id=plan.parent_id, topic=f"feedpipe: {pc.category}"
            )
            pc.channel_id = ch["id"]
            created.append(pc)
        hook = next((w for w in client.webhooks(pc.channel_id) if w.get("name") == webhook_name and w.get("token")), None)
        if hook is None:
            hook = client.create_webhook(pc.channel_id, webhook_name)
        url = f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}"
        with conn:
            conn.execute(
                "INSERT INTO discord_channels (category, guild_id, channel_id, channel_name, webhook_url, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(category) DO UPDATE SET guild_id = excluded.guild_id,"
                " channel_id = excluded.channel_id, channel_name = excluded.channel_name,"
                " webhook_url = excluded.webhook_url",
                (pc.category, guild_id, pc.channel_id, pc.name, url, db.now_iso()),
            )
    return created


def feed_categories(conn: sqlite3.Connection) -> list[str]:
    """有効なフィードのカテゴリ（なしは uncategorized）。"""
    rows = conn.execute("SELECT DISTINCT COALESCE(category, ?) FROM feeds WHERE enabled = 1 ORDER BY 1", (UNCATEGORIZED,))
    return [r[0] for r in rows]


def send_webhook(url: str, payload: dict, transport: httpx.BaseTransport | None = None) -> None:
    with httpx.Client(transport=transport, timeout=20) as c:
        for _ in range(5):
            resp = c.post(url, json=payload, params={"wait": "true"})
            if resp.status_code == 429:
                time.sleep(float(resp.json().get("retry_after", 1)))
                continue
            if resp.status_code >= 400:
                raise DiscordError(f"Webhook 送信に失敗: HTTP {resp.status_code} {resp.text[:200]}")
            return
    raise DiscordError("Webhook 送信: レート制限が解除されません")
