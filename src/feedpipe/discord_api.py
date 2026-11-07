"""Discord の Bot API を使ったフォーラム・Webhook の準備と、Webhook での投稿。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field

import httpx

from . import db
from .rss.genres import Genre, top_genre

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
GUILD_TEXT = 0
GUILD_CATEGORY = 4
GUILD_FORUM = 15

# 投稿は Webhook が行うので、Bot に必要なのはチャンネルの表示・管理、Webhook の管理と、
# 👍 の数を読むためのメッセージ履歴の閲覧だけ
PERM_VIEW_CHANNEL = 1 << 10
PERM_MANAGE_CHANNELS = 1 << 4
PERM_READ_MESSAGE_HISTORY = 1 << 16
PERM_MANAGE_WEBHOOKS = 1 << 29
PERM_MANAGE_THREADS = 1 << 34  # 投稿済みのフォーラム投稿のタグを付け直す（discord retag）
BOT_PERMISSIONS = (
    PERM_VIEW_CHANNEL | PERM_MANAGE_CHANNELS | PERM_READ_MESSAGE_HISTORY | PERM_MANAGE_WEBHOOKS | PERM_MANAGE_THREADS
)
LIKE_EMOJI = "👍"
DEFAULT_REACTION = {"emoji_id": None, "emoji_name": LIKE_EMOJI}

UNCATEGORIZED = "uncategorized"
# discord_channels で、1 日のまとめの投稿先（テキストチャンネル）を表すキー
DIGEST_KEY = "#digest"


class DiscordError(Exception):
    """Discord API が失敗したときの例外。"""


class DiscordClient:
    """Bot トークンで Discord の REST API を呼ぶクライアント（同期）。"""

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
        """HTTP 接続を閉じる。"""
        self._client.close()

    def request(self, method: str, path: str, json: object = None) -> object:
        """API を呼んで JSON を返す。429 は retry_after だけ待って再試行し、4xx/5xx は DiscordError。"""
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
        """Bot のアプリケーション ID（招待 URL に使う）。"""
        return self.request("GET", "/oauth2/applications/@me")["id"]

    def guilds(self) -> list[dict]:
        """Bot が参加しているサーバーの一覧。"""
        return self.request("GET", "/users/@me/guilds")

    def channels(self, guild_id: str) -> list[dict]:
        """サーバーのチャンネルの一覧（カテゴリ・フォーラムを含む）。"""
        return self.request("GET", f"/guilds/{guild_id}/channels")

    def create_channel(self, guild_id: str, name: str, type_: int, **extra: object) -> dict:
        """チャンネルを作る。extra はそのまま API に渡す（parent_id・topic・available_tags など）。"""
        return self.request("POST", f"/guilds/{guild_id}/channels", {"name": name, "type": type_, **extra})

    def webhooks(self, channel_id: str) -> list[dict]:
        """チャンネルの Webhook の一覧。"""
        return self.request("GET", f"/channels/{channel_id}/webhooks")

    def create_webhook(self, channel_id: str, name: str) -> dict:
        """チャンネルに Webhook を作る。"""
        return self.request("POST", f"/channels/{channel_id}/webhooks", {"name": name})


def post_url(guild_id: str, thread_id: str) -> str:
    """フォーラム投稿（スレッド）へのリンク。"""
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


def invite_url(application_id: str) -> str:
    """必要な権限（BOT_PERMISSIONS）付きの Bot 招待 URL。"""
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&scope=bot&permissions={BOT_PERMISSIONS}"
    )


def resolve_guild(client: DiscordClient, guild_id: str | None) -> dict:
    """対象のサーバーを決める。guild_id がなければ、参加しているサーバーが 1 つのときだけそれを返す。"""
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


@dataclass
class PlannedChannel:
    """setup で用意する 1 つのフォーラム（ジャンル）の予定と、既存の状態。"""
    category: str
    name: str
    feeds: list[db.Feed]
    channel_id: str | None = None  # 既存のフォーラムがあればその ID
    existing_tags: list[dict] = field(default_factory=list)
    nsfw: bool = False  # 既存のフォーラムが年齢制限か
    default_reaction: str | None = None  # 既存のフォーラムの既定のリアクション
    subgenres: dict[str, str] = field(default_factory=dict)  # サブジャンルのパス → 表示名
    description: str = ""

    @property
    def exists(self) -> bool:
        """フォーラムがすでにあるか。"""
        return self.channel_id is not None

    def _all_tags(self) -> list[str]:
        """付けたいタグ（サブジャンルの表示名、重複なし）を、上限を考えずにすべて返す。"""
        subs = [label[:MAX_TAG_CHARS].strip() for label in self.subgenres.values()]
        return list(dict.fromkeys(subs))

    def wanted_tags(self) -> list[str]:
        """サブジャンルのタグ（上限 20 個）。"""
        return self._all_tags()[:MAX_TAGS]

    def missing_tags(self) -> list[str]:
        """付けたいタグのうち、まだフォーラムにないもの。"""
        have = {t["name"] for t in self.existing_tags}
        return [n for n in self.wanted_tags() if n not in have]

    def extra_tags(self) -> list[dict]:
        """今のジャンル構成では使わない既存のタグ（setup で削除する）。"""
        wanted = set(self.wanted_tags())
        return [t for t in self.existing_tags if t["name"] not in wanted]

    @property
    def tag_overflow(self) -> int:
        """タグの上限を超えたため付けられないタグの数。"""
        return max(len(self._all_tags()) - MAX_TAGS, 0)


@dataclass
class Plan:
    """discord setup の予定全体（親カテゴリ・フォーラム・未使用のフォーラム）。"""
    guild: dict
    parent_name: str
    parent_id: str | None  # None なら親カテゴリを新しく作る
    channels: list[PlannedChannel]
    orphans: list[dict] = field(default_factory=list)  # 親カテゴリ内にあるが、どのジャンルにも対応しないフォーラム


def make_plan(
    client: DiscordClient,
    guild: dict,
    feeds_by_category: dict[str, list[db.Feed]],
    parent_name: str,
    genres: dict[str, Genre] | None = None,
) -> Plan:
    """サーバーの今の状態を読み、ジャンルごとのフォーラムの予定を作る（まだ何も変えない）。"""
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
                nsfw=bool(forum.get("nsfw")) if forum else False,
                default_reaction=(forum.get("default_reaction_emoji") or {}).get("emoji_name") if forum else None,
                subgenres={g.path: g.label for g in (genres or {}).values() if g.is_sub and g.top == cat},
                description=genres[cat].description if genres and cat in genres else "",
            )
        )
    names = {p.name for p in planned}
    orphans = [c for n, c in forums.items() if n not in names]
    return Plan(
        guild=guild,
        parent_name=parent_name,
        parent_id=parent["id"] if parent else None,
        channels=planned,
        orphans=orphans,
    )


def apply_plan(
    client: DiscordClient, conn: sqlite3.Connection, plan: Plan, webhook_name: str, *, nsfw: bool = False
) -> list[PlannedChannel]:
    """不足しているフォーラム・タグ・Webhook を作り、カテゴリとの対応を DB に保存する。作成したものを返す。

    nsfw=True ならフォーラムを年齢制限チャンネルにする（既存のものも揃える）。
    """
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
                topic=pc.description or f"feedpipe: {pc.category}",
                available_tags=[{"name": n} for n in pc.wanted_tags()],
                nsfw=nsfw,
                default_reaction_emoji=DEFAULT_REACTION,
            )
            pc.channel_id, pc.existing_tags, pc.nsfw = ch["id"], ch.get("available_tags", []), nsfw
            created.append(pc)
        else:
            changes: dict = {}
            if pc.missing_tags() or pc.extra_tags():
                # 残すタグは id 付きで渡す（id がないと別のタグとして作り直され、投稿から外れる）
                have = {t["name"]: t for t in pc.existing_tags}
                changes["available_tags"] = [have.get(n, {"name": n}) for n in pc.wanted_tags()]
            if pc.nsfw != nsfw:
                changes["nsfw"] = nsfw
            if pc.default_reaction != LIKE_EMOJI:
                # 一覧から 1 クリックで 👍 できるようにする
                changes["default_reaction_emoji"] = DEFAULT_REACTION
            if changes:
                ch = client.request("PATCH", f"/channels/{pc.channel_id}", changes)
                pc.existing_tags, pc.nsfw = ch.get("available_tags", []), bool(ch.get("nsfw"))

        tag_ids = {t["name"]: t["id"] for t in pc.existing_tags}
        # 投稿時に付けるタグ: "sub:<サブジャンル>" → タグ ID（記事のサブジャンルは LLM が判定する）
        feed_tags = {
            f"sub:{path}": tag_ids[label[:MAX_TAG_CHARS].strip()]
            for path, label in pc.subgenres.items()
            if label[:MAX_TAG_CHARS].strip() in tag_ids
        }

        register_webhook(client, conn, pc.category, guild_id, pc.channel_id, pc.name, webhook_name, feed_tags)
    return created


def register_webhook(
    client: DiscordClient,
    conn: sqlite3.Connection,
    category: str,
    guild_id: str,
    channel_id: str,
    channel_name: str,
    webhook_name: str,
    tags: dict | None = None,
) -> None:
    """チャンネルの Webhook を（なければ作って）用意し、category との対応を DB に保存する。"""
    hook = next((w for w in client.webhooks(channel_id) if w.get("name") == webhook_name and w.get("token")), None)
    if hook is None:
        hook = client.create_webhook(channel_id, webhook_name)
    url = f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}"
    with conn:
        conn.execute(
            "INSERT INTO discord_channels"
            " (category, guild_id, channel_id, channel_name, webhook_url, tags, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(category) DO UPDATE SET guild_id = excluded.guild_id,"
            " channel_id = excluded.channel_id, channel_name = excluded.channel_name,"
            " webhook_url = excluded.webhook_url, tags = excluded.tags",
            (category, guild_id, channel_id, channel_name, url, json.dumps(tags or {}), db.now_iso()),
        )


def find_text_channel(client: DiscordClient, guild_id: str, name: str) -> dict | None:
    """サーバー内のテキストチャンネルを名前で探す（カテゴリは問わない）。"""
    found = [c for c in client.channels(guild_id) if c["type"] == GUILD_TEXT and c["name"] == channel_name(name)]
    if len(found) > 1:
        raise DiscordError(f"#{name} という名前のテキストチャンネルが {len(found)} 個あります。1 つにしてください")
    return found[0] if found else None


def feeds_by_category(conn: sqlite3.Connection, only: list[str] | None = None) -> dict[str, list[db.Feed]]:
    """有効なフィードをトップレベルのジャンル（なしは uncategorized）ごとにまとめる。"""
    out: dict[str, list[db.Feed]] = {}
    for f in db.list_feeds(conn, enabled=True):
        cat = top_genre(f.category) or UNCATEGORIZED
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


def count_likes(client: DiscordClient, thread_id: str) -> int | None:
    """フォーラム投稿の 👍 の数（最初のメッセージの ID はスレッド ID と同じ）。投稿が消えていれば None。"""
    try:
        msg = client.request("GET", f"/channels/{thread_id}/messages/{thread_id}")
    except DiscordError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    return next((r["count"] for r in msg.get("reactions", []) if r["emoji"].get("name") == LIKE_EMOJI), 0)


def _webhook_request(
    method: str, url: str, payload: dict | None, transport: httpx.BaseTransport | None, *, ok_404: bool = False
) -> None:
    """Webhook のメッセージを編集・削除する。429 は待って再試行し、ok_404 なら 404 を成功とみなす。"""
    with httpx.Client(transport=transport, timeout=20) as c:
        for _ in range(5):
            resp = c.request(method, url, json=payload)
            if resp.status_code == 429:
                time.sleep(float(resp.json().get("retry_after", 1)))
                continue
            if resp.status_code == 404 and ok_404:
                return
            if resp.status_code >= 400:
                raise DiscordError(f"Webhook のメッセージ操作に失敗: HTTP {resp.status_code} {resp.text[:200]}")
            return
    raise DiscordError("Webhook: レート制限が解除されません")


def edit_webhook_message(
    webhook_url: str, message_id: str, payload: dict, transport: httpx.BaseTransport | None = None
) -> None:
    """Webhook で投稿したメッセージを書き換える（同じ Webhook で投稿したものだけ）。"""
    _webhook_request("PATCH", f"{webhook_url}/messages/{message_id}", payload, transport)


def delete_webhook_message(webhook_url: str, message_id: str, transport: httpx.BaseTransport | None = None) -> None:
    """Webhook で投稿したメッセージを消す。すでに消えていれば何もしない。"""
    _webhook_request("DELETE", f"{webhook_url}/messages/{message_id}", None, transport, ok_404=True)
