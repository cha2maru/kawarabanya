"""Discord の Bot API を使ったフォーラム・Webhook の準備と、Webhook での投稿。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from . import db

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
GUILD_TEXT = 0
GUILD_CATEGORY = 4
GUILD_FORUM = 15

# 投稿は Webhook が行うので、Bot に必要なのはチャンネルの表示・管理と Webhook の管理だけ
PERM_VIEW_CHANNEL = 1 << 10
PERM_MANAGE_CHANNELS = 1 << 4
PERM_MANAGE_WEBHOOKS = 1 << 29
BOT_PERMISSIONS = PERM_VIEW_CHANNEL | PERM_MANAGE_CHANNELS | PERM_MANAGE_WEBHOOKS

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


MAX_TAGS = 20  # フォーラムに付けられるタグの上限
MAX_TAG_CHARS = 20


def tag_name(feed: db.Feed) -> str:
    name = (feed.title or urlsplit(feed.url).hostname or "feed").strip()
    return name[:MAX_TAG_CHARS]


@dataclass
class PlannedChannel:
    category: str
    name: str
    feeds: list[db.Feed]
    channel_id: str | None = None  # 既存のフォーラムがあればその ID
    existing_tags: list[dict] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return self.channel_id is not None

    def wanted_tags(self) -> list[str]:
        """フィード名のタグ（重複除去、上限 20 個）。"""
        names = list(dict.fromkeys(tag_name(f) for f in self.feeds))
        return names[:MAX_TAGS]

    def missing_tags(self) -> list[str]:
        have = {t["name"] for t in self.existing_tags}
        room = MAX_TAGS - len(have)
        return [n for n in self.wanted_tags() if n not in have][: max(room, 0)]

    @property
    def tag_overflow(self) -> int:
        """タグの上限を超えたためタグなしで投稿されるフィードの数。"""
        names = {tag_name(f) for f in self.feeds}
        return max(len(names) - MAX_TAGS, 0)


@dataclass
class Plan:
    guild: dict
    parent_name: str
    parent_id: str | None  # None なら親カテゴリを新しく作る
    channels: list[PlannedChannel]


def make_plan(
    client: DiscordClient, guild: dict, feeds_by_category: dict[str, list[db.Feed]], parent_name: str
) -> Plan:
    existing = client.channels(guild["id"])
    parent = next(
        (c for c in existing if c["type"] == GUILD_CATEGORY and c["name"].lower() == parent_name.lower()),
        None,
    )
    forums = {
        c["name"]: c
        for c in existing
        if c["type"] == GUILD_FORUM and parent and c.get("parent_id") == parent["id"]
    }
    planned = []
    for cat, feeds in feeds_by_category.items():
        name = channel_name(cat)
        forum = forums.get(name)
        planned.append(
            PlannedChannel(
                category=cat,
                name=name,
                feeds=feeds,
                channel_id=forum["id"] if forum else None,
                existing_tags=forum.get("available_tags", []) if forum else [],
            )
        )
    return Plan(guild=guild, parent_name=parent_name, parent_id=parent["id"] if parent else None, channels=planned)


def apply_plan(client: DiscordClient, conn: sqlite3.Connection, plan: Plan, webhook_name: str) -> list[PlannedChannel]:
    """不足しているフォーラム・タグ・Webhook を作り、カテゴリとの対応を DB に保存する。作成したものを返す。"""
    guild_id = plan.guild["id"]
    if plan.parent_id is None:
        plan.parent_id = client.create_channel(guild_id, plan.parent_name, GUILD_CATEGORY)["id"]

    created = []
    for pc in plan.channels:
        if not pc.exists:
            ch = client.create_channel(
                guild_id,
                pc.name,
                GUILD_FORUM,
                parent_id=plan.parent_id,
                topic=f"feedpipe: {pc.category}",
                available_tags=[{"name": n} for n in pc.wanted_tags()],
            )
            pc.channel_id, pc.existing_tags = ch["id"], ch.get("available_tags", [])
            created.append(pc)
        elif missing := pc.missing_tags():
            # 既存のタグは id 付きで渡さないと消えてしまう
            ch = client.request(
                "PATCH",
                f"/channels/{pc.channel_id}",
                {"available_tags": [*pc.existing_tags, *({"name": n} for n in missing)]},
            )
            pc.existing_tags = ch.get("available_tags", [])

        tag_ids = {t["name"]: t["id"] for t in pc.existing_tags}
        feed_tags = {str(f.id): tag_ids[n] for f in pc.feeds if (n := tag_name(f)) in tag_ids}

        hook = next((w for w in client.webhooks(pc.channel_id) if w.get("name") == webhook_name and w.get("token")), None)
        if hook is None:
            hook = client.create_webhook(pc.channel_id, webhook_name)
        url = f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}"
        with conn:
            conn.execute(
                "INSERT INTO discord_channels"
                " (category, guild_id, channel_id, channel_name, webhook_url, tags, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(category) DO UPDATE SET guild_id = excluded.guild_id,"
                " channel_id = excluded.channel_id, channel_name = excluded.channel_name,"
                " webhook_url = excluded.webhook_url, tags = excluded.tags",
                (pc.category, guild_id, pc.channel_id, pc.name, url, json.dumps(feed_tags), db.now_iso()),
            )
    return created


def feeds_by_category(conn: sqlite3.Connection, only: list[str] | None = None) -> dict[str, list[db.Feed]]:
    """有効なフィードをカテゴリ（なしは uncategorized）ごとにまとめる。"""
    out: dict[str, list[db.Feed]] = {}
    for f in db.list_feeds(conn, enabled=True):
        cat = f.category or UNCATEGORIZED
        if only is None or cat in only:
            out.setdefault(cat, []).append(f)
    for cat in only or []:
        out.setdefault(cat, [])
    return dict(sorted(out.items()))


def send_webhook(url: str, payload: dict, transport: httpx.BaseTransport | None = None) -> dict:
    """Webhook で投稿し、作成されたメッセージを返す（フォーラムなら channel_id がスレッド ID）。"""
    with httpx.Client(transport=transport, timeout=20) as c:
        for _ in range(5):
            resp = c.post(url, json=payload, params={"wait": "true"})
            if resp.status_code == 429:
                time.sleep(float(resp.json().get("retry_after", 1)))
                continue
            if resp.status_code >= 400:
                raise DiscordError(f"Webhook 送信に失敗: HTTP {resp.status_code} {resp.text[:200]}")
            return resp.json()
    raise DiscordError("Webhook 送信: レート制限が解除されません")
