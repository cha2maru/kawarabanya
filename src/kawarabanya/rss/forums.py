"""ジャンルに合わせて Discord のフォーラム・タグ・Webhook を揃える（discord setup）。

ジャンル（genres.toml のトップレベル）ごとにフォーラム 1 つ、サブジャンルごとにタグ 1 つ。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from kawarabanya.core import db
from kawarabanya.core.discord_api import (
    DEFAULT_REACTION,
    GUILD_CATEGORY,
    GUILD_FORUM,
    LIKE_EMOJI,
    UNCATEGORIZED,
    DiscordClient,
    channel_name,
    register_webhook,
)
from kawarabanya.rss.genres import Genre, top_genre

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
                topic=pc.description or f"kawarabanya: {pc.category}",
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


