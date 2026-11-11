"""投稿先（[chat] targets の Discord・Slack）に共通のコマンド（送り先の一覧・テスト投稿・タグの付け直し・重複の削除）。"""

from __future__ import annotations

import argparse
import sys

from kawarabanya.cli.common import _fit, _load_genres, _open_db
from kawarabanya.core import chat as chatdb
from kawarabanya.core.chat import Article
from kawarabanya.core.config import Config
from kawarabanya.core.targets import build_chats


def cmd_chat_channels(cfg: Config, args: argparse.Namespace) -> int:
    """chat channels: 登録済みの送り先（サービス・ジャンル → チャンネル）を表示する。"""
    for ch in chatdb.channels(_open_db(cfg, args)):
        print(f"{ch.platform:<8} {_fit(ch.key, 16)} #{ch.name}  ({ch.channel_id})")
    return 0

def cmd_chat_retag(cfg: Config, args: argparse.Namespace) -> int:
    """chat retag: 投稿済みの記事のタグ（Slack はラベル）を、サブジャンルに付け直す（未判定なら LLM で判定）。"""
    import asyncio

    from kawarabanya.core.llm import Summarizer
    from kawarabanya.rss.notify import retag_posts

    conn = _open_db(cfg, args)
    chats = build_chats(cfg)

    async def go():
        async with Summarizer(cfg.summarize) as summarizer:
            return await retag_posts(conn, chats, summarizer, _load_genres(cfg))

    changed, total = asyncio.run(go())
    print(f"タグを付け直しました: {changed} / {total} 件")
    return 0


def cmd_chat_delete_duplicates(cfg: Config, args: argparse.Namespace) -> int:
    """chat delete-duplicates: 重複と判定した記事の投稿を、targets のすべてから消す（--apply なしは一覧だけ）。"""
    from kawarabanya.rss.notify import delete_duplicate_posts

    rows = delete_duplicate_posts(_open_db(cfg, args), build_chats(cfg), apply=args.apply)
    for r in rows:
        print(f"  {r['id']:>5} {r['platform']:<8} {r['title']}")
    print(f"重複の投稿: {len(rows)} 件" + ("を削除しました" if args.apply else "（--apply で削除）"))
    return 0


def cmd_chat_test(cfg: Config, args: argparse.Namespace) -> int:
    """chat test: ジャンルの送り先（targets のすべて）にテスト投稿する。"""
    conn = _open_db(cfg, args)
    sent = 0
    for c in build_chats(cfg):
        if (ch := chatdb.channel(conn, c.platform, args.category)) is None:
            print(f"{c.platform}: {args.category} の送り先が未登録です（discord setup・slack setup --apply）", file=sys.stderr)
            continue
        c.post_article(ch, Article("kawarabanya テスト", "kawarabanya", args.message, None, None, args.category))
        print(f"{c.platform}: #{ch.name} に送信しました")
        sent += 1
    return 0 if sent else 1
