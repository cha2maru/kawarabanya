"""コマンドラインインターフェース。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import db, opml
from .check import CheckResult, Status, apply_results, check_feeds
from .config import Config, load_config
from .run import FeedReport, run_feeds

STATUS_MARK = {
    Status.OK: "OK  ",
    Status.NOT_FEED: "NOTF",
    Status.GONE: "GONE",
    Status.FORBIDDEN: "DENY",
    Status.HTTP_ERROR: "HTTP",
    Status.DNS: "DNS ",
    Status.NETWORK: "NET ",
}


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _fit(s: str, width: int) -> str:
    """全角文字を 2 桁として、指定幅に切り詰め・空白埋めする。"""
    out, w = "", 0
    for c in s:
        cw = _width(c)
        if w + cw > width:
            if out:
                out = out[:-1] if _width(out[-1]) == 1 else out[:-1] + " "
                out += "…"
            break
        out += c
        w += cw
    return out + " " * (width - _width(out))


def _open_db(cfg: Config, args: argparse.Namespace) -> sqlite3.Connection:
    return db.connect(args.db or cfg.general.db)


# --- import / export -------------------------------------------------------


def cmd_import_opml(cfg: Config, args: argparse.Namespace) -> int:
    parsed = opml.parse_opml(args.file)
    conn = _open_db(cfg, args)
    stats = opml.import_entries(conn, parsed.entries, update=args.update)
    print(f"{args.file}: フィード {len(parsed.entries)} 件（重複 {len(parsed.duplicates)} 件は除外）")
    print(f"  追加 {stats.added} / 更新 {stats.updated} / 既存のためスキップ {stats.skipped}")
    if args.verbose:
        for url, category in parsed.duplicates:
            print(f"  重複: [{category}] {url}")
    return 0


def cmd_export_opml(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_db(cfg, args)
    feeds = db.list_feeds(conn, enabled=None if args.all else True)
    opml.export_opml(feeds, args.file)
    print(f"{args.file}: {len(feeds)} 件を書き出しました")
    return 0


# --- check -----------------------------------------------------------------


def _print_result(r: CheckResult) -> None:
    title = r.feed.title or r.feed.url
    line = f"[{STATUS_MARK[r.status]}] {r.feed.id:>4} {_fit(title, 36)} "
    if r.status.is_ok:
        latest = r.latest_entry.astimezone().strftime("%Y-%m-%d") if r.latest_entry else "日付なし"
        line += f"{r.entries:>3} 件  最新 {latest}"
    else:
        line += r.error or ""
    if r.moved_to:
        line += f"  → 移転: {r.moved_to}"
    print(line, flush=True)


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_db(cfg, args)
    if args.id:
        feeds = [f for i in args.id if (f := db.get_feed(conn, i))]
    else:
        feeds = db.list_feeds(conn, enabled=None if args.all else True, category=args.category)
    if not feeds:
        print("対象のフィードがありません", file=sys.stderr)
        return 1

    print(f"{len(feeds)} 件をチェックします…", file=sys.stderr)
    on_result = None if args.quiet else _print_result
    results = asyncio.run(check_feeds(feeds, cfg.general, on_result=on_result))

    counts = Counter(r.status for r in results)
    moved = sum(1 for r in results if r.moved_to)
    summary = " / ".join(f"{s.value} {counts[s]}" for s in Status if counts[s])
    stale_before = datetime.now(UTC) - timedelta(days=cfg.general.stale_days)
    stale = [
        r for r in results
        if r.status.is_ok and (r.entries == 0 or (r.latest_entry and r.latest_entry < stale_before))
    ]
    print(f"\n結果: {summary}（うち移転 {moved}）")
    if stale:
        print(f"注意: 記事が 0 件か、{cfg.general.stale_days} 日以上更新のないフィードが {len(stale)} 件あります")

    if args.dry_run:
        print("--dry-run のため DB は更新していません")
        return 0
    stats = apply_results(
        conn, results, max_failures=cfg.general.max_failures, disable_dead=args.disable_dead
    )
    print(
        f"DB 更新: URL 変更 {stats.moved} / 無効化 {stats.disabled}"
        f" / 移転先が登録済みのため無効化 {stats.merged}"
    )
    if not args.disable_dead:
        dead = sum(1 for r in results if r.status.is_permanent)
        if dead:
            print(f"ヒント: 回復の見込みが薄いもの {dead} 件は --disable-dead で無効化できます")
    return 0


# --- run -------------------------------------------------------------------


def _select_feeds(conn: sqlite3.Connection, args: argparse.Namespace) -> list[db.Feed]:
    if args.id:
        return [f for i in args.id if (f := db.get_feed(conn, i))]
    return db.list_feeds(conn, enabled=True, category=args.category)


def _print_report(r: FeedReport) -> None:
    title = _fit(r.feed.title or r.feed.url, 36)
    if not r.check.status.is_ok:
        print(f"[{STATUS_MARK[r.check.status]}] {r.feed.id:>4} {title} {r.check.error}", flush=True)
        return
    if r.initialized:
        print(f"[INIT] {r.feed.id:>4} {title} 既存 {r.check.entries} 件を既読化"
              + (f"（うち {len(r.saved)} 件を保存）" if r.saved else ""), flush=True)
    elif r.new:
        print(f"[NEW ] {r.feed.id:>4} {title} 新着 {r.new} 件 / 保存 {len(r.saved)} 件", flush=True)
    for it in r.saved:
        print(f"         + [{it.content_source}] {it.title}")
    for it, err in r.errors:
        print(f"         ! {it.title}: {err}")


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_db(cfg, args)
    feeds = _select_feeds(conn, args)
    if not feeds:
        print("対象のフィードがありません", file=sys.stderr)
        return 1
    if args.no_fulltext:
        cfg.fulltext.enabled = False

    print(f"{len(feeds)} 件を取得します…", file=sys.stderr)
    reports = asyncio.run(
        run_feeds(conn, feeds, cfg, backfill=args.backfill, on_report=None if args.quiet else _print_report)
    )
    failed = sum(1 for r in reports if not r.check.status.is_ok)
    unchanged = sum(1 for r in reports if r.check.not_modified)
    inited = sum(1 for r in reports if r.initialized)
    saved = sum(len(r.saved) for r in reports)
    errors = sum(len(r.errors) for r in reports)
    print(
        f"\n結果: 保存 {saved} 件 / 記事エラー {errors} 件"
        f"（フィード: 初回 {inited} / 更新なし {unchanged} / 取得失敗 {failed}）"
    )
    return 1 if errors else 0


# --- feeds -----------------------------------------------------------------


def cmd_feeds_list(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_db(cfg, args)
    enabled = None if args.all else (not args.disabled)
    feeds = db.list_feeds(conn, enabled=enabled, category=args.category)
    for f in feeds:
        state = "on " if f.enabled else "off"
        status = f.last_status or "-"
        latest = (
            datetime.fromisoformat(f.latest_entry_at).astimezone().strftime("%Y-%m-%d")
            if f.latest_entry_at
            else "-" * 10
        )
        extra = f"  ({f.disabled_reason})" if f.disabled_reason else ""
        print(
            f"{f.id:>4} {state} {status:<10} {latest} {_fit(f.category or '-', 14)} "
            f"{_fit(f.title or '-', 36)} {f.url}{extra}"
        )
    print(f"{len(feeds)} 件", file=sys.stderr)
    return 0


def cmd_feeds_add(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_db(cfg, args)
    if existing := db.find_feed_by_url(conn, args.url):
        print(f"登録済みです: {existing.id} {existing.url}", file=sys.stderr)
        return 1
    with conn:
        feed_id = db.insert_feed(conn, args.url, args.title, None, args.category)
    print(f"追加しました: {feed_id} {args.url}")
    return 0


def cmd_feeds_option(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_db(cfg, args)
    if args.value is None:
        value = None
    else:
        try:
            value = json.loads(args.value)
        except json.JSONDecodeError:
            value = args.value
    with conn:
        if not db.set_option(conn, args.id, args.name, value):
            print(f"{args.id}: 見つかりません", file=sys.stderr)
            return 1
    print(f"{args.id}: {json.dumps(db.get_feed(conn, args.id).options, ensure_ascii=False)}")
    return 0


def _cmd_set_enabled(cfg: Config, args: argparse.Namespace, enabled: bool) -> int:
    conn = _open_db(cfg, args)
    rc = 0
    with conn:
        for feed_id in args.ids:
            if db.set_enabled(conn, feed_id, enabled, "manual"):
                print(f"{feed_id}: {'有効' if enabled else '無効'}にしました")
            else:
                print(f"{feed_id}: 見つかりません", file=sys.stderr)
                rc = 1
    return rc


# --- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="feedpipe", description="RSS を全文 Markdown で保存し通知する")
    p.add_argument("-c", "--config", type=Path, help="設定ファイル（既定: ./config.toml）")
    p.add_argument("--db", help="DB ファイル（設定ファイルの値を上書き）")
    p.add_argument("-v", "--verbose", action="store_true", help="詳細なログを出す")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("import-opml", help="OPML を取り込む")
    s.add_argument("file", type=Path)
    s.add_argument("--update", action="store_true", help="登録済みフィードのタイトル・カテゴリを更新する")
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
    s.add_argument("-q", "--quiet", action="store_true", help="結果の集計だけ表示する")
    s.set_defaults(func=cmd_run)

    feeds = sub.add_parser("feeds", help="フィードの管理").add_subparsers(dest="feeds_command", required=True)

    s = feeds.add_parser("list", help="一覧")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--disabled", action="store_true", help="無効なものだけ")
    g.add_argument("--all", action="store_true", help="すべて")
    s.add_argument("--category")
    s.set_defaults(func=cmd_feeds_list)

    s = feeds.add_parser("add", help="追加")
    s.add_argument("url")
    s.add_argument("--title")
    s.add_argument("--category")
    s.set_defaults(func=cmd_feeds_add)

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

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # trafilatura は抽出できなかったページごとに ERROR を出すので、-v のとき以外は抑える
    logging.getLogger("trafilatura").setLevel(logging.INFO if args.verbose else logging.CRITICAL)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
        return args.func(cfg, args)
    except (OSError, ValueError, ET.ParseError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
