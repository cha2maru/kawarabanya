"""常駐 Bot。Discord に接続したまま、定期的に `kawarabanya run` などを別プロセスで実行する。

Bot へのメンション（日本語の依頼）は assistant でコマンドに直し、変更するものはボタンで確認してから実行する。
使えるのはサーバーの持ち主だけ。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
import shlex
import sys

import discord
from discord.ext import tasks

from . import assistant, db
from .config import Config
from .llm import Summarizer
from .rss.pipeline import load_genres_if_any

# Discord のメッセージの上限（2,000 字）に収めるため、コマンドの出力はこの字数で切る
MAX_OUTPUT = 1800

log = logging.getLogger(__name__)


class Bot(discord.Client):
    """常駐 Bot 本体。Discord に接続したまま、定期的に run・genres suggest・digest を別プロセスで実行する。"""

    def __init__(self, cfg: Config, config_args: list[str]):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.config_args = config_args  # 子プロセスにも同じ --config / --db を渡す
        self.llm: asyncio.subprocess.Process | None = None
        # 毎時の実行・定時の実行・依頼されたコマンドが重ならないよう、1 つずつ実行する
        self._lock = asyncio.Lock()
        self.summarizer: Summarizer | None = None
        args = list(config_args)
        self.db_path = args[args.index("--db") + 1] if "--db" in args else cfg.general.db

    async def setup_hook(self) -> None:
        """接続前の準備。要約サーバーの起動と、定期実行の開始を行う。"""
        if cmd := self.cfg.bot.llm_server_command:
            self.llm = await asyncio.create_subprocess_exec(
                *shlex.split(cmd), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            log.info("要約サーバーを起動しました (pid %s)", self.llm.pid)
        self.summarizer = Summarizer(self.cfg.summarize)
        if self.cfg.bot.run_interval_minutes > 0:
            self.periodic_run.change_interval(minutes=self.cfg.bot.run_interval_minutes)
            self.periodic_run.start()
        if self.cfg.digest.enabled and self.cfg.digest.times:
            # まとめの時刻（6:30・18:30）ちょうどにも実行する
            tz = datetime.datetime.now().astimezone().tzinfo
            times = [datetime.time(*map(int, t.split(":")), tzinfo=tz) for t in self.cfg.digest.times]
            self.scheduled_run = tasks.loop(time=times)(self.cycle)
            self.scheduled_run.before_loop(self._wait_ready)
            self.scheduled_run.start()

    async def on_ready(self) -> None:
        """ログインできたことをログに残す。"""
        log.info("%s としてログインしました（サーバー %d 件）", self.user, len(self.guilds))

    @tasks.loop(minutes=60)
    async def periodic_run(self) -> None:
        """毎時の定期実行。"""
        await self.cycle()

    async def cycle(self) -> None:
        """定期実行の 1 回分。run で記事を取り込み、まとめの時刻を過ぎていれば、サブジャンルを作ってからまとめを投稿する。"""
        async with self._lock:
            await self._kawarabanya("run", "-q", *self.cfg.bot.run_args)
            # 要約サーバーを run と取り合わないよう、run の後に続けて実行する。
            # 新しいサブジャンルを先に作り、振り分け直した記事をまとめに反映する（まとめの時刻ごと）
            await self._kawarabanya("genres", "suggest", "--apply", "--due")
            await self._kawarabanya("digest", "--due")

    async def _kawarabanya(self, *args: str) -> tuple[int, str]:
        """kawarabanya のサブコマンドを別プロセスで実行し、出力をログに流す。(終了コード, 出力) を返す。

        要約で数十分かかることがあるので、イベントループを止めないよう別プロセスにする。
        """
        cmd = [sys.executable, "-m", "kawarabanya", *self.config_args, *args]
        log.info("実行: %s", shlex.join(cmd[1:]))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="replace")
        for line in text.splitlines():
            if line.strip():
                log.info("%s: %s", args[0], line)
        if proc.returncode:
            log.warning("%s が終了コード %s で終わりました", args[0], proc.returncode)
        return proc.returncode or 0, text

    async def on_message(self, message: discord.Message) -> None:
        """Bot へのメンションを日本語の依頼として受け、コマンドに直して返事をする（サーバーの持ち主だけ）。"""
        if message.author.bot or self.user is None or self.user not in message.mentions:
            return
        if message.guild is None or message.author.id != message.guild.owner_id:
            await message.reply("この Bot に頼めるのはサーバーの持ち主だけです", mention_author=False)
            return
        text = re.sub(r"<@!?\d+>", "", message.content).strip()
        if not text:
            await message.reply(assistant.help_text(), mention_author=False)
            return
        conn = db.connect(self.db_path)
        try:
            genres = load_genres_if_any(self.cfg) or {}
            async with message.channel.typing():
                data = await assistant.interpret(self.summarizer, text, genres) if self.summarizer else None
            intent = assistant.build(data, text, conn, genres)
        finally:
            conn.close()
        log.info("依頼: %s → %s %s", text, intent.command, intent.argv)
        if intent.error:
            await message.reply(intent.error, mention_author=False)
        elif not intent.argv:
            await message.reply(intent.summary, mention_author=False)
        elif intent.confirm:
            view = ConfirmView(self, intent, message.author.id)
            view.message = await message.reply(
                f"**{intent.summary}**\n`kawarabanya {shlex.join(intent.argv)}` を実行します", view=view,
                mention_author=False,
            )
        else:
            reply = await message.reply(f"{intent.summary}を調べています…", mention_author=False)
            await reply.edit(content=await self.run_requested(intent))

    async def run_requested(self, intent: assistant.Intent) -> str:
        """依頼されたコマンドを（定期実行と重ならないよう順番に）実行し、返事の本文を返す。"""
        async with self._lock:
            rc, out = await self._kawarabanya(*intent.argv)
        out = out.strip() or "（出力なし）"
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + "\n…（以下略）"
        return f"{'✅' if rc == 0 else '⚠️'} {intent.summary}\n```\n{out}\n```"

    @periodic_run.before_loop
    async def _wait_ready(self) -> None:
        """Discord への接続が終わるまで、最初の定期実行を待たせる。"""
        await self.wait_until_ready()

    async def close(self) -> None:
        """Bot を止める。一緒に起動した要約サーバーも止める。"""
        if self.summarizer:
            await self.summarizer.__aexit__()
        if self.llm and self.llm.returncode is None:
            self.llm.terminate()
            await self.llm.wait()
        await super().close()


class ConfirmView(discord.ui.View):
    """変更するコマンドの確認ボタン（依頼した本人だけが押せる。10 分で無効）。"""

    def __init__(self, bot: Bot, intent: assistant.Intent, user_id: int):
        super().__init__(timeout=600)
        self.bot, self.intent, self.user_id = bot, intent, user_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("依頼した人だけが押せます", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, content: str) -> None:
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=content, view=self)

    @discord.ui.button(label="実行", style=discord.ButtonStyle.primary)
    async def run(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        waiting = "（定期実行が終わるのを待っています）" if self.bot._lock.locked() else ""
        await self._finish(interaction, f"**{self.intent.summary}** を実行中…{waiting}")
        await interaction.edit_original_response(content=await self.bot.run_requested(self.intent))

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._finish(interaction, f"~~{self.intent.summary}~~（やめました）")

    async def on_timeout(self) -> None:
        if self.message:
            for item in self.children:
                item.disabled = True
            await self.message.edit(content=f"~~{self.intent.summary}~~（時間切れ）", view=self)


def run_bot(cfg: Config, config_args: list[str]) -> None:
    """常駐 Bot を起動し、止められるまで戻らない。トークンがなければ ValueError。"""
    token = cfg.discord.token
    if not token:
        raise ValueError(f"{cfg.discord.token_env} が設定されていません（.env に書いてください）")
    # discord.py のログ設定は使わず、kawarabanya の logging 設定に任せる
    Bot(cfg, config_args).run(token, log_handler=None)
