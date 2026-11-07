"""常駐 Bot。Discord に接続したまま、定期的に `feedpipe run` を別プロセスで実行する。

スラッシュコマンドなどの対話機能は、この Client に足していく。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import shlex
import sys

import discord
from discord.ext import tasks

from .config import Config

log = logging.getLogger(__name__)


class Bot(discord.Client):
    """常駐 Bot 本体。Discord に接続したまま、定期的に run・genres suggest・digest を別プロセスで実行する。"""

    def __init__(self, cfg: Config, config_args: list[str]):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.config_args = config_args  # 子プロセスにも同じ --config / --db を渡す
        self.llm: asyncio.subprocess.Process | None = None
        # 毎時の実行と定時の実行が重ならないよう、1 つずつ実行する
        self._lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        """接続前の準備。要約サーバーの起動と、定期実行の開始を行う。"""
        if cmd := self.cfg.bot.llm_server_command:
            self.llm = await asyncio.create_subprocess_exec(
                *shlex.split(cmd), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            log.info("要約サーバーを起動しました (pid %s)", self.llm.pid)
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
            await self._feedpipe("run", "-q", *self.cfg.bot.run_args)
            # 要約サーバーを run と取り合わないよう、run の後に続けて実行する。
            # 新しいサブジャンルを先に作り、振り分け直した記事をまとめに反映する（まとめの時刻ごと）
            await self._feedpipe("genres", "suggest", "--apply", "--due")
            await self._feedpipe("digest", "--due")

    async def _feedpipe(self, *args: str) -> None:
        """feedpipe のサブコマンドを別プロセスで実行し、出力をログに流す。

        要約で数十分かかることがあるので、イベントループを止めないよう別プロセスにする。
        """
        cmd = [sys.executable, "-m", "feedpipe", *self.config_args, *args]
        log.info("定期実行: %s", shlex.join(cmd[1:]))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        for line in out.decode(errors="replace").splitlines():
            if line.strip():
                log.info("%s: %s", args[0], line)
        if proc.returncode:
            log.warning("%s が終了コード %s で終わりました", args[0], proc.returncode)

    @periodic_run.before_loop
    async def _wait_ready(self) -> None:
        """Discord への接続が終わるまで、最初の定期実行を待たせる。"""
        await self.wait_until_ready()

    async def close(self) -> None:
        """Bot を止める。一緒に起動した要約サーバーも止める。"""
        if self.llm and self.llm.returncode is None:
            self.llm.terminate()
            await self.llm.wait()
        await super().close()


def run_bot(cfg: Config, config_args: list[str]) -> None:
    """常駐 Bot を起動し、止められるまで戻らない。トークンがなければ ValueError。"""
    token = cfg.discord.token
    if not token:
        raise ValueError(f"{cfg.discord.token_env} が設定されていません（.env に書いてください）")
    # discord.py のログ設定は使わず、feedpipe の logging 設定に任せる
    Bot(cfg, config_args).run(token, log_handler=None)
