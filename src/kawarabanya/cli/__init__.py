"""コマンドラインインターフェース（引数の定義と main）。"""

from __future__ import annotations

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from kawarabanya.cli.doctor import cmd_doctor
from kawarabanya.cli.rss import (
    _cmd_set_enabled,
    cmd_check,
    cmd_digest,
    cmd_export_opml,
    cmd_feeds_add,
    cmd_feeds_list,
    cmd_feeds_option,
    cmd_feeds_remove,
    cmd_feeds_rsshub,
    cmd_genres_apply,
    cmd_genres_interest,
    cmd_genres_list,
    cmd_genres_suggest,
    cmd_import_opml,
    cmd_interests,
    cmd_likes,
    cmd_notify,
    cmd_run,
    cmd_status,
    cmd_summarize,
)
from kawarabanya.cli.server import (
    cmd_bot,
    cmd_discord_channels,
    cmd_discord_delete_duplicates,
    cmd_discord_guilds,
    cmd_discord_invite_url,
    cmd_discord_retag,
    cmd_discord_setup,
    cmd_discord_test,
)
from kawarabanya.core import discord_api
from kawarabanya.core.config import load_config, load_dotenv
from kawarabanya.rss.sources import extensions


def build_parser() -> argparse.ArgumentParser:
    """サブコマンドと引数を定義した ArgumentParser を返す。"""
    p = argparse.ArgumentParser(prog="kawarabanya", description="RSS を全文 Markdown で保存し通知する")
    p.add_argument("-c", "--config", type=Path, help="設定ファイル（既定: ./config.toml）")
    p.add_argument("--db", help="DB ファイル（設定ファイルの値を上書き）")
    p.add_argument("-v", "--verbose", action="store_true", help="詳細なログを出す")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("import-opml", help="OPML を取り込む（genres.toml のジャンルにも書き足す）")
    s.add_argument("file", type=Path)
    s.add_argument("--update", action="store_true", help="登録済みフィードのタイトル・カテゴリを更新する")
    s.add_argument("--genre", help="新しいフィードをすべてこのジャンルに入れる。auto ならフォルダ名で決まらないものを LLM が決める（省略時はフォルダ名と同じジャンルだけ）")
    s.set_defaults(func=cmd_import_opml)

    s = sub.add_parser("export-opml", help="OPML に書き出す")
    s.add_argument("file", type=Path)
    s.add_argument("--all", action="store_true", help="無効なフィードも含める")
    s.set_defaults(func=cmd_export_opml)

    s = sub.add_parser("check", help="フィードの死活チェック")
    s.add_argument("--category", help="このカテゴリだけ")
    s.add_argument("--id", type=int, nargs="+", help="このフィード ID だけ")
    s.add_argument("--all", action="store_true", help="無効なフィードも含める")
    s.add_argument("--disable-dead", action="store_true", help="404/410・ホスト消滅・非フィードを即無効化する")
    s.add_argument("--dry-run", action="store_true", help="DB を更新しない")
    s.add_argument("-q", "--quiet", action="store_true", help="結果の集計だけ表示する")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("run", help="新着記事を取得して保存する")
    s.add_argument("--category", help="このカテゴリだけ")
    s.add_argument("--id", type=int, nargs="+", help="このフィード ID だけ")
    s.add_argument("--backfill", type=int, default=0, metavar="N",
                   help="初回のフィードは既読化に加えて、新しい順に N 件を保存する")
    s.add_argument("--no-fulltext", action="store_true", help="元記事を取りに行かず、フィードの本文だけ使う")
    s.add_argument("--no-notify", action="store_true", help="Discord に通知しない（次回以降に送られる）")
    s.add_argument("--no-summary", action="store_true", help="LLM で要約しない")
    s.add_argument("-q", "--quiet", action="store_true", help="結果の集計だけ表示する")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("summarize", help="保存済み・未通知で要約のない最近の記事を要約する")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_summarize)

    s = sub.add_parser("notify", help="保存済みで未通知の記事を Discord に投稿する")
    s.add_argument("--limit", type=int, help="投稿する上限（既定: [discord] max_posts_per_run）")
    s.add_argument("--category", help="このカテゴリだけ")
    s.add_argument("--dry-run", action="store_true", help="送らずに内容を表示する")
    s.set_defaults(func=cmd_notify)

    s = sub.add_parser("digest", help="ジャンルごとのまとめ（前回のまとめ以降の記事）を今すぐ投稿する")
    s.add_argument("--genre", nargs="+", help="このジャンルだけ")
    s.add_argument("--due", action="store_true", help="[digest] times の直近の時刻より後に投稿済みなら何もしない（Bot の定期実行用）")
    s.add_argument("--dry-run", action="store_true", help="投稿せずに内容を表示する")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--update", action="store_true", help="最後に投稿したまとめを、最新の内容で書き換える")
    g.add_argument("--delete", action="store_true", help="ジャンルごとに最後のまとめを削除する")
    s.add_argument("--day", help="--delete で、この日（YYYY-MM-DD）のまとめをすべて消す")
    s.set_defaults(func=cmd_digest)

    s = sub.add_parser("doctor", help="動かすのに必要なもの（設定・DB・Discord・要約サーバー）がそろっているかを調べる")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("status", help="最後の取り込み・次のまとめ・要約サーバー・フィードと記事の状態")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("bot", help="常駐 Bot を起動する（定期的に run を実行）")
    s.set_defaults(func=cmd_bot)

    s = sub.add_parser("likes", help="投稿の 👍 の数を今すぐ取得する（run でも 6 時間ごとに取得）")
    s.set_defaults(func=cmd_likes)

    s = sub.add_parser("interests", help="👍 から興味の傾向を表示する")
    s.add_argument("--days", type=int, default=30, help="集計する期間（日）")
    s.add_argument("--top", type=int, default=15, help="フィード・記事を表示する件数")
    s.set_defaults(func=cmd_interests)

    feeds = sub.add_parser("feeds", help="フィードの管理").add_subparsers(dest="feeds_command", required=True)

    s = feeds.add_parser("list", help="一覧")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--disabled", action="store_true", help="無効なものだけ")
    g.add_argument("--all", action="store_true", help="すべて")
    s.add_argument("--category")
    s.set_defaults(func=cmd_feeds_list)

    s = feeds.add_parser("add", help="サイトかフィードの URL から登録する（RSS を自動で探し、genres.toml にも書く）")
    s.add_argument("url")
    s.add_argument("--genre", help="ジャンルかサブジャンル（news、hobby/boardgame など）。auto なら LLM が決める")
    s.add_argument("--title")
    s.add_argument("--no-discover", action="store_true", help="フィードを探さず、URL をそのまま登録する")
    s.set_defaults(func=cmd_feeds_add)

    s = feeds.add_parser("rsshub", help="URL が RSSHub のどのルートで取れるかを調べる（RSSHub は動かさなくてよい）")
    s.add_argument("url")
    s.set_defaults(func=cmd_feeds_rsshub)

    s = feeds.add_parser("remove", help="無効にして genres.toml から外す（保存済みの記事と投稿は残す）")
    s.add_argument("id", help="フィードの ID か URL")
    s.set_defaults(func=cmd_feeds_remove)

    s = feeds.add_parser("option", help="フィード個別の設定（例: no_fulltext true）。値を省略すると削除")
    s.add_argument("id", type=int)
    s.add_argument("name")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_feeds_option)

    s = feeds.add_parser("enable", help="有効にする")
    s.add_argument("ids", type=int, nargs="+")
    s.set_defaults(func=lambda c, a: _cmd_set_enabled(c, a, True))

    s = feeds.add_parser("disable", help="無効にする")
    s.add_argument("ids", type=int, nargs="+")
    s.set_defaults(func=lambda c, a: _cmd_set_enabled(c, a, False))

    gs = sub.add_parser("genres", help="ジャンル（genres.toml）の管理").add_subparsers(dest="genres_command", required=True)
    s = gs.add_parser("list", help="ジャンルの一覧とフィード数")
    s.set_defaults(func=cmd_genres_list)
    s = gs.add_parser("apply", help="genres.toml の割り当てをフィードに反映する")
    s.add_argument("--dry-run", action="store_true", help="DB を更新しない")
    s.add_argument("--disable-unassigned", action="store_true", help="genres.toml にないフィードを無効にする")
    s.set_defaults(func=cmd_genres_apply)
    s = gs.add_parser("interest", help="ジャンル・サブジャンルの好き嫌いを変える")
    s.add_argument("genre", help="news、culture/sports など")
    s.add_argument("level", choices=["like", "normal", "down", "low", "mute"],
                   help="like: まとめの上位に / normal / down: まとめの下の方に / low: まとめに載せない / mute: 投稿もしない")
    s.set_defaults(func=cmd_genres_interest)
    s = gs.add_parser("suggest", help="当てはまるサブジャンルのなかった記事から、新しいサブジャンルを LLM に考えさせる")
    s.add_argument("--apply", action="store_true", help="genres.toml に追加し、Discord のタグも作る")
    s.add_argument("--days", type=int, default=7, help="直近この日数の記事から考える（既定 7）")
    s.add_argument("--due", action="store_true", help="まとめの時刻を過ぎていて、今日まだ実行していなければ実行する（Bot 用）")
    s.set_defaults(func=cmd_genres_suggest)

    dc = sub.add_parser("discord", help="Discord の準備（Bot を使う）").add_subparsers(dest="discord_command", required=True)
    s = dc.add_parser("invite-url", help="Bot をサーバーに招待する URL を表示")
    s.set_defaults(func=cmd_discord_invite_url)
    s = dc.add_parser("guilds", help="Bot が参加しているサーバーの一覧")
    s.set_defaults(func=cmd_discord_guilds)
    s = dc.add_parser("setup", help="カテゴリごとのフォーラムとタグ・Webhook を作る（既定は確認だけ）")
    s.add_argument("--categories", nargs="+", help="対象のカテゴリ（既定: 有効なフィードの全カテゴリ）")
    s.add_argument("--apply", action="store_true", help="実際に作成する")
    s.add_argument("--prune", action="store_true", help="--apply と一緒に使い、ジャンルに対応しないフォーラムを削除する（投稿も消える）")
    s.set_defaults(func=cmd_discord_setup)
    s = dc.add_parser("channels", help="登録済みのチャンネル")
    s.set_defaults(func=cmd_discord_channels)
    s = dc.add_parser("retag", help="投稿済みの記事のタグを、サブジャンルに付け直す（未判定なら LLM で判定）")
    s.set_defaults(func=cmd_discord_retag)
    s = dc.add_parser("delete-duplicates", help="重複と判定した記事のフォーラムの投稿を消す")
    s.add_argument("--apply", action="store_true", help="実際に消す（なしは一覧だけ）")
    s.set_defaults(func=cmd_discord_delete_duplicates)
    s = dc.add_parser("test", help="カテゴリのチャンネルにテスト送信")
    s.add_argument("category")
    s.add_argument("message", nargs="?", default="kawarabanya からのテスト送信です")
    s.set_defaults(func=cmd_discord_test)

    return p

def main(argv: list[str] | None = None) -> int:
    """引数を解釈してサブコマンドを実行し、終了コードを返す。

    想定内のエラー（設定・ファイル・Discord API）はメッセージだけを表示して 1 を返す。
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # trafilatura は抽出できなかったページごとに ERROR を出すので、-v のとき以外は抑える
    logging.getLogger("trafilatura").setLevel(logging.INFO if args.verbose else logging.CRITICAL)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.INFO if args.verbose else logging.WARNING)
    # 音声機能は使わないので、PyNaCl などがないという警告は出さない
    logging.getLogger("discord.client").setLevel(logging.INFO if args.verbose else logging.ERROR)
    if args.command == "bot":
        # 常駐中は何をしたかを残したいので、-v がなくても kawarabanya 自身のログは INFO で出す
        logging.getLogger("kawarabanya").setLevel(logging.INFO)
    try:
        load_dotenv()
        cfg = load_config(args.config)
        extensions.DIR = Path(cfg.general.extensions_dir)
        return args.func(cfg, args)
    except (OSError, ValueError, ET.ParseError, discord_api.DiscordError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
