"""常駐 Bot。Discord に接続したまま、定期的に `feedpipe run` を別プロセスで実行する。

スラッシュコマンドなどの対話機能は、この Client に足していく。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import sys

import discord
from discord.ext import tasks

from .config import Config

log = logging.getLogger(__name__)


class Bot(discord.Client):
    def __init__(self, cfg: Config, config_args: list[str]):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.config_args = config_args  # 子プロセスにも同じ --config / --db を渡す
        self.llm: asyncio.subprocess.Process | None = None

    async def setup_hook(self) -> None:
        if cmd := self.cfg.bot.llm_server_command:
            self.llm = await asyncio.create_subprocess_exec(
                *shlex.split(cmd), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            log.info("要約サーバーを起動しました (pid %s)", self.llm.pid)
        if self.cfg.bot.run_interval_minutes > 0:
            self.periodic_run.change_interval(minutes=self.cfg.bot.run_interval_minutes)
            self.periodic_run.start()

    async def on_ready(self) -> None:
        log.info("%s としてログインしました（サーバー %d 件）", self.user, len(self.guilds))

    @tasks.loop(minutes=60)
    async def periodic_run(self) -> None:
        # 要約で数十分かかることがあるので、イベントループを止めないよう別プロセスで実行する
        cmd = [sys.executable, "-m", "feedpipe", *self.config_args, "run", "-q", *self.cfg.bot.run_args]
        log.info("定期実行: %s", shlex.join(cmd[1:]))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        for line in out.decode(errors="replace").splitlines():
            if line.strip():
                log.info("run: %s", line)
        if proc.returncode:
            log.warning("run が終了コード %s で終わりました", proc.returncode)

    @periodic_run.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()

    async def close(self) -> None:
        if self.llm and self.llm.returncode is None:
            self.llm.terminate()
            await self.llm.wait()
        await super().close()


def run_bot(cfg: Config, config_args: list[str]) -> None:
    token = cfg.discord.token
    if not token:
        raise ValueError(f"{cfg.discord.token_env} が設定されていません（.env に書いてください）")
    # discord.py のログ設定は使わず、feedpipe の logging 設定に任せる
    Bot(cfg, config_args).run(token, log_handler=None)
