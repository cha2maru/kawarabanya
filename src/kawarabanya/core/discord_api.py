"""Discord の REST API（Bot トークン）と Webhook の薄いクライアント。チャンネル・Webhook の登録、投稿・編集・削除。"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import time

import httpx

from kawarabanya.core.chat import (
    UNCATEGORIZED,
    Article,
    Channel,
    ChatError,
    Posted,
    PostGoneError,
    article_text,
    save_channel,
)

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
PERM_SEND_MESSAGES = 1 << 11  # メンションされた依頼に返事をする
PERM_SEND_MESSAGES_IN_THREADS = 1 << 38  # 記事の投稿のスレッドで頼まれた依頼に返事をする
BOT_PERMISSIONS = (
    PERM_VIEW_CHANNEL | PERM_MANAGE_CHANNELS | PERM_READ_MESSAGE_HISTORY | PERM_MANAGE_WEBHOOKS | PERM_MANAGE_THREADS
    | PERM_SEND_MESSAGES | PERM_SEND_MESSAGES_IN_THREADS
)
LIKE_EMOJI = "👍"
DISLIKE_EMOJI = "👎"
DEFAULT_REACTION = {"emoji_id": None, "emoji_name": LIKE_EMOJI}



class DiscordError(ChatError):
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
                "User-Agent": "DiscordBot (https://github.com/, 0.1) kawarabanya",
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
    """kawarabanya のカテゴリを Discord のチャンネル名にする（Discord 側の正規化に合わせる）。"""
    name = (category or UNCATEGORIZED).lower()
    name = re.sub(r"[\s/]+", "-", name)
    name = re.sub(r"[^\w\-ー]", "", name).strip("-")
    return name[:100] or UNCATEGORIZED


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
    save_channel(conn, Channel("discord", category, channel_id, channel_name, url, guild_id, tags or {}))


def find_text_channel(client: DiscordClient, guild_id: str, name: str) -> dict | None:
    """サーバー内のテキストチャンネルを名前で探す（カテゴリは問わない）。"""
    found = [c for c in client.channels(guild_id) if c["type"] == GUILD_TEXT and c["name"] == channel_name(name)]
    if len(found) > 1:
        raise DiscordError(f"#{name} という名前のテキストチャンネルが {len(found)} 個あります。1 つにしてください")
    return found[0] if found else None


def create_private_text_channel(client: DiscordClient, guild_id: str, name: str) -> dict:
    """持ち主と Bot だけが見られるテキストチャンネルを作る（@everyone から隠す。持ち主は常に見える）。"""
    me = client.request("GET", "/users/@me")["id"]
    allow = PERM_VIEW_CHANNEL | PERM_SEND_MESSAGES | PERM_MANAGE_WEBHOOKS | PERM_READ_MESSAGE_HISTORY
    return client.create_channel(guild_id, name, GUILD_TEXT, permission_overwrites=[
        {"id": guild_id, "type": 0, "allow": "0", "deny": str(PERM_VIEW_CHANNEL)},
        {"id": me, "type": 1, "allow": str(allow), "deny": "0"},
    ])


def with_mention(payload: dict, user_id: str) -> dict:
    """payload の本文の先頭に user_id へのメンションを付け、そのユーザーにだけ通知が届くようにする（空なら何もしない）。"""
    if not user_id:
        return payload
    return {**payload, "content": f"<@{user_id}> {payload.get('content', '')}"[:2000],
            "allowed_mentions": {"parse": [], "users": [user_id]}}


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


def count_reactions(client: DiscordClient, thread_id: str) -> tuple[int, int] | None:
    """フォーラム投稿の (👍, 👎) の数（最初のメッセージの ID はスレッド ID と同じ）。投稿が消えていれば None。"""
    try:
        msg = client.request("GET", f"/channels/{thread_id}/messages/{thread_id}")
    except DiscordError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    counts = {r["emoji"].get("name"): r["count"] for r in msg.get("reactions", [])}
    return counts.get(LIKE_EMOJI, 0), counts.get(DISLIKE_EMOJI, 0)


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


MAX_THREAD_NAME = 100
MAX_CONTENT = 2000
MAX_USERNAME = 80


def _username(name: str | None) -> str:
    """Webhook の表示名（フィード名）。Discord の制限で「discord」を含む名前は使えない。"""
    name = (name or "kawarabanya").strip()[:MAX_USERNAME]
    return "kawarabanya" if "discord" in name.lower() else name


def _check(e: DiscordError, need: str = "スレッドの管理") -> None:
    """404 なら PostGoneError、403 なら足りない権限を添えて投げ直す。"""
    if "HTTP 404" in str(e):
        raise PostGoneError(str(e)) from None
    if "HTTP 403" in str(e):
        raise DiscordError(f"{e}（Bot に「{need}」の権限が必要です）") from None
    raise e


class DiscordChat:
    """Discord への投稿（記事 = フォーラムの投稿、サブジャンル = タグ、まとめ・予定 = テキストチャンネルへの Webhook）。

    投稿・まとめは Webhook で送る。タグの付け替え・削除・返信・👍 の読み取りは Bot トークンの client を使う。
    """

    platform = "discord"

    def __init__(self, client: DiscordClient | None = None, *, mention_user_id: str = "",
                 transport: httpx.BaseTransport | None = None):
        self.client = client
        self.mention_user_id = mention_user_id
        self.transport = transport

    def _api(self) -> DiscordClient:
        if self.client is None:
            raise DiscordError("Bot のトークンが設定されていません（.env の DISCORD_BOT_TOKEN）")
        return self.client

    def payload(self, ch: Channel, a: Article) -> dict:
        """記事 1 件をフォーラムに投稿する Webhook の内容（スレッド名・本文・タグ）。"""
        out: dict = {
            "thread_name": a.title[:MAX_THREAD_NAME],
            "username": _username(a.feed_title),
            "content": article_text(a, MAX_CONTENT),
            # 記事中の @everyone などで通知が飛ばないようにする
            "allowed_mentions": {"parse": []},
        }
        if tag := ch.tags.get(f"sub:{a.subgenre}"):
            out["applied_tags"] = [tag]
        return out

    def post_article(self, ch: Channel, a: Article) -> Posted:
        """フォーラムに投稿する（Webhook の応答の channel_id が投稿のスレッド ID）。"""
        ref = send_webhook(ch.webhook_url, self.payload(ch, a), transport=self.transport)["channel_id"]
        return Posted(ref, post_url(ch.guild_id, ref) if ch.guild_id else None)

    def retag(self, ch: Channel, ref: str, a: Article) -> None:
        """投稿のタグを付け替える。"""
        try:
            self._api().request("PATCH", f"/channels/{ref}", {"applied_tags": [t for t in [ch.tags.get(f"sub:{a.subgenre}")] if t]})
        except DiscordError as e:
            _check(e)

    def delete_post(self, ref: str) -> None:
        """投稿（スレッド）を消す。"""
        try:
            self._api().request("DELETE", f"/channels/{ref}")
        except DiscordError as e:
            with contextlib.suppress(PostGoneError):
                _check(e)

    def reply(self, ref: str, text: str) -> int:
        """投稿のスレッドに、2,000 字ずつ返信する。"""
        sent = 0
        for i in range(0, len(text), MAX_CONTENT):
            self._api().request("POST", f"/channels/{ref}/messages",
                                {"content": text[i:i + MAX_CONTENT], "allowed_mentions": {"parse": []}})
            sent += 1
        return sent

    def reactions(self, ref: str) -> tuple[int, int] | None:
        """投稿の (👍, 👎) の数。"""
        return count_reactions(self._api(), ref)

    def send(self, ch: Channel, content: str, *, username: str, mention: bool = False) -> str:
        """テキストチャンネルに Webhook で送る。"""
        payload = {"username": username, "content": content[:MAX_CONTENT], "allowed_mentions": {"parse": []}}
        if mention:
            payload = with_mention(payload, self.mention_user_id)
        return send_webhook(ch.webhook_url, payload, transport=self.transport)["id"]

    def edit(self, ch: Channel, ref: str, content: str) -> None:
        """send で送ったメッセージを書き換える。"""
        edit_webhook_message(ch.webhook_url, ref, {"content": content[:MAX_CONTENT], "allowed_mentions": {"parse": []}},
                             transport=self.transport)

    def remove(self, ch: Channel, ref: str) -> None:
        """send で送ったメッセージを消す。"""
        delete_webhook_message(ch.webhook_url, ref, transport=self.transport)
