"""[chat] targets から、投稿するチャット（DiscordChat・SlackChat）を作る。"""

from __future__ import annotations

from kawarabanya.core import discord_api, slack_api
from kawarabanya.core.chat import Chat
from kawarabanya.core.config import Config


def build_chats(cfg: Config) -> list[Chat]:
    """[chat] targets のチャット。Discord は投稿に Webhook を使うので、Bot のトークンがなくても投稿だけはできる。"""
    out: list[Chat] = []
    for t in cfg.chat.targets:
        if t == "discord":
            client = discord_api.DiscordClient(cfg.discord.token) if cfg.discord.token else None
            out.append(discord_api.DiscordChat(client, mention_user_id=cfg.discord.mention_user_id))
        else:
            if not cfg.slack.token:
                raise ValueError(f"Slack の Bot のトークンがありません（.env の {cfg.slack.token_env}。README の「Slack」）")
            out.append(slack_api.SlackChat(slack_api.SlackClient(cfg.slack.token),
                                           mention_user_id=cfg.slack.mention_user_id))
    return out
