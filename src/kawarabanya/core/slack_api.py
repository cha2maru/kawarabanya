"""Slack の Web API（Bot トークン）の薄いクライアントと、Slack への投稿（SlackChat）。

Slack にはフォーラムもタグもないので、ジャンルごとにチャンネル（kawarabanya-tech など）を作り、1 記事 = 1 メッセージ、
サブジャンルは本文の先頭の [ラベル]。表示名はフィード名（chat:write.customize）。既定のリアクションがないので、
投稿のたびに Bot が 👍・👎 を付けておき、数えるときに Bot の分を引く。
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time

import httpx

from kawarabanya.core.chat import Article, Channel, ChatError, Posted, PostGoneError, article_text

log = logging.getLogger(__name__)

API = "https://slack.com/api/"
MAX_TEXT = 3000
LIKE_NAME = "+1"
DISLIKE_NAME = "-1"
# 投稿が消えていることを表すエラー
GONE = {"message_not_found", "channel_not_found", "thread_not_found", "is_archived"}
# Bot に要る権限（OAuth & Permissions → Bot Token Scopes）
SCOPES = (
    "chat:write", "chat:write.customize", "channels:read", "channels:manage", "channels:join", "groups:read",
    "groups:write", "reactions:read", "reactions:write", "app_mentions:read", "channels:history", "groups:history",
)


class SlackError(ChatError):
    """Slack の API が失敗したときの例外（error は Slack のエラー名）。"""

    def __init__(self, method: str, error: str):
        super().__init__(f"{method}: {error}")
        self.error = error


class SlackClient:
    """Bot トークンで Slack の Web API を呼ぶクライアント（同期）。"""

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=API, transport=transport, timeout=20, headers={"Authorization": f"Bearer {token}"}
        )
        self._me: dict | None = None

    def call(self, method: str, **params: object) -> dict:
        """API を呼んで JSON を返す。429 は Retry-After だけ待って再試行し、ok でなければ SlackError。"""
        # 配列・オブジェクトの引数は JSON の文字列で送る（フォーム形式ならどのメソッドも受け付ける）
        data = {k: json.dumps(v) if isinstance(v, (list, dict)) else str(v).lower() if isinstance(v, bool) else v
                for k, v in params.items() if v is not None}
        for _ in range(5):
            resp = self._client.post(method, data=data)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 1))
                log.info("Slack のレート制限のため %.1f 秒待ちます", wait)
                time.sleep(wait)
                continue
            body = resp.json()
            if not body.get("ok"):
                raise SlackError(method, body.get("error", f"HTTP {resp.status_code}"))
            return body
        raise SlackError(method, "rate_limited")

    def me(self) -> dict:
        """Bot 自身（auth.test: user_id・team_id・url）。"""
        if self._me is None:
            self._me = self.call("auth.test")
        return self._me

    def channels(self) -> list[dict]:
        """Bot から見えるチャンネル（公開・非公開。アーカイブ済みは除く）。"""
        out, cursor = [], None
        while True:
            r = self.call("conversations.list", types="public_channel,private_channel", exclude_archived=True,
                          limit=200, cursor=cursor)
            out += r["channels"]
            if not (cursor := r.get("response_metadata", {}).get("next_cursor")):
                return out


def escape(text: str) -> str:
    """Slack の本文で特別な意味を持つ & < > を書き換える。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def from_markdown(text: str) -> str:
    """Discord の Markdown（見出し・太字・[文字](URL)）を Slack の書き方にする。"""
    text = escape(text)
    text = re.sub(r"^#{1,3} (.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    return re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f"<{m[2]}|{m[1].replace('|', '｜')}>", text)


def split_ref(ref: str) -> tuple[str, str]:
    """投稿の ID（"チャンネル/ts"）を (チャンネル, ts) にする。"""
    channel, ts = ref.split("/", 1)
    return channel, ts


def channel_name(prefix: str, key: str) -> str:
    """Slack のチャンネル名（小文字・空白とスラッシュはハイフン・80 字まで）。"""
    return re.sub(r"[\s/.]+", "-", f"{prefix}{key}".lower())[:80]


class SlackChat:
    """Slack への投稿（記事 = チャンネルの 1 メッセージ、サブジャンル = 本文の先頭の [ラベル]）。"""

    platform = "slack"

    def __init__(self, client: SlackClient, *, mention_user_id: str = ""):
        self.client = client
        self.mention_user_id = mention_user_id

    def text(self, ch: Channel, a: Article) -> str:
        """記事の本文（[ラベル] *見出し* と、要約・公開日時・URL）。"""
        label = ch.tags.get(f"sub:{a.subgenre}")
        head = (f"[{escape(label)}] " if label else "") + f"*{escape(a.title)}*"
        return f"{head}\n{escape(article_text(a, MAX_TEXT - len(head) - 1))}"

    def link(self, channel: str, ts: str) -> str:
        """メッセージへのリンク（ワークスペースの URL から組み立てる。getPermalink を呼ばずに済む）。"""
        return f"{self.client.me()['url']}archives/{channel}/p{ts.replace('.', '')}"

    def post_article(self, ch: Channel, a: Article) -> Posted:
        """記事を投稿し、押しやすいように 👍・👎 を付けておく。"""
        r = self.client.call("chat.postMessage", channel=ch.channel_id, text=self.text(ch, a),
                             username=(a.feed_title or "kawarabanya")[:80], unfurl_links=False, unfurl_media=False)
        for name in (LIKE_NAME, DISLIKE_NAME):
            self.client.call("reactions.add", channel=r["channel"], timestamp=r["ts"], name=name)
        return Posted(f"{r['channel']}/{r['ts']}", self.link(r["channel"], r["ts"]))

    def _gone(self, f, *args, **kw) -> dict:
        try:
            return f(*args, **kw)
        except SlackError as e:
            if e.error in GONE:
                raise PostGoneError(str(e)) from None
            raise

    def retag(self, ch: Channel, ref: str, a: Article) -> None:
        """本文の [ラベル] を書き換える。"""
        channel, ts = split_ref(ref)
        self._gone(self.client.call, "chat.update", channel=channel, ts=ts, text=self.text(ch, a))

    def delete_post(self, ref: str) -> None:
        """投稿を消す（すでに消えていれば何もしない）。"""
        channel, ts = split_ref(ref)
        with contextlib.suppress(PostGoneError):
            self._gone(self.client.call, "chat.delete", channel=channel, ts=ts)

    def reply(self, ref: str, text: str) -> int:
        """投稿のスレッドに返信する。"""
        channel, ts = split_ref(ref)
        sent = 0
        for i in range(0, len(text), MAX_TEXT):
            self.client.call("chat.postMessage", channel=channel, thread_ts=ts, text=escape(text[i:i + MAX_TEXT]))
            sent += 1
        return sent

    def reactions(self, ref: str) -> tuple[int, int] | None:
        """投稿の (👍, 👎) の数（Bot が付けておいた分は引く）。消えていれば None。"""
        channel, ts = split_ref(ref)
        try:
            r = self._gone(self.client.call, "reactions.get", channel=channel, timestamp=ts, full=True)
        except PostGoneError:
            return None
        me = self.client.me()["user_id"]
        counts = {x["name"]: x["count"] - (me in x.get("users", [])) for x in r["message"].get("reactions", [])}
        return counts.get(LIKE_NAME, 0), counts.get(DISLIKE_NAME, 0)

    def send(self, ch: Channel, content: str, *, username: str, mention: bool = False) -> str:
        """まとめ・予定・お知らせを送る。"""
        text = from_markdown(content)[:MAX_TEXT]
        if mention and self.mention_user_id:
            text = f"<@{self.mention_user_id}> {text}"
        r = self.client.call("chat.postMessage", channel=ch.channel_id, text=text, username=username,
                             unfurl_links=False, unfurl_media=False)
        return f"{r['channel']}/{r['ts']}"

    def edit(self, ch: Channel, ref: str, content: str) -> None:
        """send で送ったメッセージを書き換える。"""
        channel, ts = split_ref(ref)
        self.client.call("chat.update", channel=channel, ts=ts, text=from_markdown(content)[:MAX_TEXT])

    def remove(self, ch: Channel, ref: str) -> None:
        """send で送ったメッセージを消す。"""
        self.delete_post(ref)
