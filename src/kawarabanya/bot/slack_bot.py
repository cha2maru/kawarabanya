"""Slack の受け口（Socket Mode）。@ への依頼、確認ボタン、👍・👎 のリアクションを受ける。

Socket Mode は Bot から Slack につなぎに行くので、外から届く URL を公開しなくてよい（自宅の WSL のままで動く）。
依頼をコマンドに直す・実行するのは Discord と同じ Bot（bot/client.py）の make_intent・run_command。
頼めるのは [slack] mention_user_id の人（持ち主）だけ。
"""

from __future__ import annotations

import logging
import re
import shlex
import time
import uuid
from typing import TYPE_CHECKING

from kawarabanya.bot import assistant
from kawarabanya.core import chat as chatdb
from kawarabanya.core import db
from kawarabanya.core.slack_api import DISLIKE_NAME, LIKE_NAME, from_markdown

if TYPE_CHECKING:
    from kawarabanya.bot.client import Bot

log = logging.getLogger(__name__)

CONFIRM_SECONDS = 600  # 確認ボタンが使える時間（Discord と同じ 10 分）


class SlackBridge:
    """Slack の Socket Mode でイベントを受け、Bot に渡す。"""

    def __init__(self, bot: Bot, web, bot_user_id: str, owner: str):
        self.bot = bot
        self.web = web  # slack_sdk の AsyncWebClient（テストでは同じ形の偽物）
        self.bot_user_id = bot_user_id
        self.owner = owner
        self.pending: dict[str, tuple[assistant.Intent, float]] = {}  # 確認待ちの依頼

    async def handle(self, kind: str, payload: dict) -> None:
        """Socket Mode の 1 件（events_api か interactive）を処理する。"""
        if kind == "events_api":
            event = payload.get("event", {})
            if event.get("type") in ("reaction_added", "reaction_removed"):
                self.count_reaction(event, 1 if event["type"] == "reaction_added" else -1)
            elif event.get("type") == "app_mention":
                await self.on_mention(event)
        elif kind == "interactive" and payload.get("type") == "block_actions":
            await self.on_button(payload)

    def count_reaction(self, event: dict, delta: int) -> None:
        """記事の投稿に付いた・外れた 👍・👎 を数える（Bot が先に付けた分は数えない）。"""
        item = event.get("item", {})
        if event.get("user") == self.bot_user_id or item.get("type") != "message":
            return
        name = event.get("reaction", "").split("::")[0]  # 肌の色の違い（+1::skin-tone-2）も同じに数える
        kind = {LIKE_NAME: chatdb.LIKE, DISLIKE_NAME: chatdb.DISLIKE}.get(name, "")
        conn = db.connect(self.bot.db_path)
        try:
            ref = f"{item['channel']}/{item['ts']}"
            if chatdb.count_reaction(conn, "slack", ref, kind, delta):
                log.info("Slack のリアクション %s %+d: 投稿 %s", name, delta, ref)
        finally:
            conn.close()

    async def say(self, event: dict, text: str, blocks: list | None = None) -> dict:
        """依頼のスレッドに返事をする。"""
        return await self.web.chat_postMessage(
            channel=event["channel"], thread_ts=event.get("thread_ts") or event["ts"], text=text, blocks=blocks
        )

    async def on_mention(self, event: dict) -> None:
        """@kawarabanya への依頼を受け、コマンドに直して返事をする（持ち主だけ）。"""
        if not self.owner or event.get("user") != self.owner:
            await self.say(event, "この Bot に頼めるのは持ち主（config.toml の [slack] mention_user_id）だけです")
            return
        text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
        if not text:
            await self.say(event, from_markdown(assistant.help_text()))
            return
        # スレッドの中の依頼は、スレッドの元の投稿（記事）についての依頼
        ref = f"{event['channel']}/{event.get('thread_ts') or event['ts']}"
        intent = await self.bot.make_intent(text, "slack", ref)
        if intent.error or not intent.argv:
            await self.say(event, from_markdown(intent.error or intent.summary))
        elif intent.confirm:
            key = uuid.uuid4().hex
            self.pending[key] = (intent, time.monotonic())
            line = f"*{intent.summary}*\n`kawarabanya {shlex.join(intent.argv)}` を実行します"
            await self.say(event, line, blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": line}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "実行"}, "style": "primary",
                     "action_id": "run", "value": key},
                    {"type": "button", "text": {"type": "plain_text", "text": "やめる"}, "action_id": "cancel",
                     "value": key},
                ]},
            ])
        else:
            msg = await self.say(event, f"{intent.summary}を調べています…")
            await self.web.chat_update(channel=msg["channel"], ts=msg["ts"],
                                       text=from_markdown(await self.bot.run_command(intent)))

    async def on_button(self, payload: dict) -> None:
        """確認ボタン（実行・やめる）。押せるのは持ち主だけ、10 分まで。"""
        action = payload["actions"][0]
        channel, ts = payload["channel"]["id"], payload["message"]["ts"]
        if payload["user"]["id"] != self.owner:
            return
        found = self.pending.pop(action.get("value", ""), None)
        if found is None or time.monotonic() - found[1] > CONFIRM_SECONDS:
            await self.web.chat_update(channel=channel, ts=ts, text="（時間切れか、実行済みです）", blocks=[])
            return
        intent = found[0]
        if action["action_id"] != "run":
            await self.web.chat_update(channel=channel, ts=ts, text=f"~{intent.summary}~（やめました）", blocks=[])
            return
        await self.web.chat_update(channel=channel, ts=ts, text=f"*{intent.summary}* を実行中…", blocks=[])
        content = from_markdown(await self.bot.run_command(intent))
        if intent.attach:
            content += f"\n（ファイル: {intent.attach}）"  # shortcut: Slack にはファイルを添付しない。要るなら files_upload_v2
        await self.web.chat_update(channel=channel, ts=ts, text=content, blocks=[])


async def connect(bot: Bot) -> object:
    """Socket Mode で Slack につなぎ、イベントを Bot に渡し始める。つないだクライアントを返す。"""
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web.async_client import AsyncWebClient

    cfg = bot.cfg.slack
    web = AsyncWebClient(token=cfg.token)
    me = await web.auth_test()
    bridge = SlackBridge(bot, web, me["user_id"], cfg.mention_user_id)
    client = SocketModeClient(app_token=cfg.app_token, web_client=web)

    async def listener(client, req) -> None:
        # 3 秒以内に受け取ったと返す（返さないと Slack が送り直す）。処理はそのあと
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        try:
            await bridge.handle(req.type, req.payload)
        except Exception:  # 1 件の失敗で受け口を止めない
            log.exception("Slack のイベントの処理に失敗: %s", req.type)

    client.socket_mode_request_listeners.append(listener)
    await client.connect()
    log.info("Slack に Socket Mode でつなぎました（%s）", me.get("team"))
    return client
