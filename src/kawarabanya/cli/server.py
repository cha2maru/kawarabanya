"""Discord サーバーの管理と常駐 Bot のコマンド。"""

from __future__ import annotations

import argparse
import sys

from kawarabanya.cli.common import _discord_client, _load_genres, _open_db
from kawarabanya.core import discord_api
from kawarabanya.core.chat import DIGEST_KEY, SCHEDULE_KEY
from kawarabanya.core.config import Config
from kawarabanya.rss import forums


def cmd_discord_invite_url(cfg: Config, args: argparse.Namespace) -> int:
    """discord invite-url: 必要な権限付きの Bot 招待 URL を表示する。"""
    client = _discord_client(cfg)
    print(discord_api.invite_url(client.application_id()))
    print("権限: チャンネルを見る / チャンネルの管理 / ウェブフックの管理 / メッセージ履歴を読む / スレッドの管理 / メッセージを送信", file=sys.stderr)
    return 0

def cmd_discord_guilds(cfg: Config, args: argparse.Namespace) -> int:
    """discord guilds: Bot が参加しているサーバーを表示する。"""
    for g in _discord_client(cfg).guilds():
        print(f"{g['id']}  {g['name']}")
    return 0

def cmd_discord_setup(cfg: Config, args: argparse.Namespace) -> int:
    """discord setup: ジャンルに合わせてフォーラム・タグ・Webhook とまとめの投稿先を揃える。

    --apply がなければ予定を表示するだけ。--prune でジャンルに対応しないフォーラムを削除する。
    """
    conn = _open_db(cfg, args)
    client = _discord_client(cfg)
    guild = discord_api.resolve_guild(client, cfg.discord.guild_id)
    genre_defs = _load_genres(cfg)
    only = args.categories
    if only is None and genre_defs:
        # genres.toml があれば、そこで定義したトップレベルのジャンルだけフォーラムにする
        only = [g.path for g in genre_defs.values() if not g.is_sub]
    by_cat = forums.feeds_by_category(conn, only)
    plan = forums.make_plan(client, guild, by_cat, cfg.discord.parent_category, genre_defs)

    print(f"サーバー: {guild['name']}")
    parent_state = "既存" if plan.parent_id else "新規作成"
    print(f"カテゴリ: {plan.parent_name}（{parent_state}）  年齢制限: {'オン' if cfg.discord.nsfw else 'オフ'}")
    for pc in plan.channels:
        if pc.exists:
            todo = []
            if missing := pc.missing_tags():
                todo.append(f"タグ追加 {len(missing)}")
            if extra := pc.extra_tags():
                todo.append(f"タグ削除 {len(extra)}: {'、'.join(t['name'] for t in extra)}")
            if pc.nsfw != cfg.discord.nsfw:
                todo.append("年齢制限" + ("オン" if cfg.discord.nsfw else "オフ"))
            if pc.default_reaction != discord_api.LIKE_EMOJI:
                todo.append(f"既定リアクション {discord_api.LIKE_EMOJI}")
            if pc.archive_minutes != forums.ARCHIVE_MINUTES:
                todo.append(f"投稿が閉じるまで {forums.ARCHIVE_MINUTES // 60} 時間")
            state = f"既存（{'、'.join(todo)}）" if todo else "既存"
        else:
            state = "作成"
        tags = "、".join(pc.wanted_tags())
        print(f"  {state:<10} 💬{pc.name:<16} ← {pc.category}（{len(pc.feeds)} フィード）")
        print(f"             タグ: {tags or '-'}")
        if pc.tag_overflow:
            print(f"             ※タグの上限 {forums.MAX_TAGS} 個を超える {pc.tag_overflow} フィードはタグなしで投稿します")
    digest_ch = None
    if cfg.digest.enabled:
        digest_ch = discord_api.find_text_channel(client, guild["id"], cfg.digest.channel)
        state = "既存" if digest_ch else "見つかりません（Discord で作ってください。まとめは投稿されません）"
        print(f"  まとめ      #{cfg.digest.channel}（{state}）")
    schedule_ch = None
    if cfg.schedule.enabled:
        schedule_ch = discord_api.find_text_channel(client, guild["id"], cfg.schedule.channel)
        state = "既存" if schedule_ch else "新規作成（持ち主と Bot だけが見られる非公開のチャンネル）"
        print(f"  予定        #{cfg.schedule.channel}（{state}）")
    for c in plan.orphans:
        print(f"  {'削除' if args.prune else '未使用'}      💬{c['name']}（どのジャンルにも対応しません）")
    if not args.apply:
        print("\n--apply を付けると作成します（既存のフォーラムはタグと Webhook だけ用意します。--prune で未使用を削除）")
        return 0
    created = forums.apply_plan(client, conn, plan, cfg.discord.webhook_name, nsfw=cfg.discord.nsfw)
    if args.prune:
        for c in plan.orphans:
            client.request("DELETE", f"/channels/{c['id']}")
        print(f"未使用のフォーラムを {len(plan.orphans)} 件削除しました")
    if digest_ch:
        discord_api.register_webhook(
            client, conn, DIGEST_KEY, guild["id"], digest_ch["id"], digest_ch["name"], cfg.discord.webhook_name
        )
    if cfg.schedule.enabled and schedule_ch is None:
        schedule_ch = discord_api.create_private_text_channel(client, guild["id"], cfg.schedule.channel)
    if schedule_ch:
        discord_api.register_webhook(
            client, conn, SCHEDULE_KEY, guild["id"], schedule_ch["id"], schedule_ch["name"],
            cfg.discord.webhook_name,
        )
    if genre_defs and not args.categories:
        # ジャンルに対応しなくなった送り先を消す（未割り当てのフィードが古いフォーラムに投稿されないように）
        names = [pc.category for pc in plan.channels] + [DIGEST_KEY, SCHEDULE_KEY]
        with conn:
            conn.execute(
                f"DELETE FROM channels WHERE platform = 'discord' AND key NOT IN ({','.join('?' * len(names))})", names
            )
    print(f"\nフォーラムを {len(created)} 件作成し、{len(plan.channels)} 件の Webhook を登録しました")
    return 0

def cmd_discord_archive(cfg: Config, args: argparse.Namespace) -> int:
    """discord archive: 最後の動きから --hours 時間たった開いている投稿を閉じる（開いているスレッドの上限 1,000 対策）。"""
    conn = _open_db(cfg, args)
    client = _discord_client(cfg)
    guild = discord_api.resolve_guild(client, cfg.discord.guild_id)
    print(f"{forums.archive_old_posts(client, conn, guild['id'], args.hours)} 件の投稿を閉じました")
    return 0

def cmd_bot(cfg: Config, args: argparse.Namespace) -> int:
    """bot: 常駐 Bot を起動する。子プロセスにも同じ --config / --db を渡す。"""
    from kawarabanya.bot.client import run_bot

    config_args = [*(["--config", str(args.config)] if args.config else []), *(["--db", args.db] if args.db else [])]
    run_bot(cfg, config_args)
    return 0
