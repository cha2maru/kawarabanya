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

from . import db, discord_api, genres, notify, opml
from .check import CheckResult, Status, apply_results, check_feeds
from .config import Config, load_config, load_dotenv
from .run import FeedReport, run_feeds, summarize_pending
from .summarize import Summarizer

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
        print(f"         + [{it.content_source}{' 要約' if it.summary else ''}] {it.title}")
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
    async def go():
        if not cfg.summarize.enabled or args.no_summary:
            return await run_feeds(conn, feeds, cfg, backfill=args.backfill, on_report=on_report)
        async with Summarizer(cfg.summarize) as summarizer:
            return await run_feeds(
                conn, feeds, cfg, backfill=args.backfill, summarizer=summarizer, on_report=on_report
            )

    on_report = None if args.quiet else _print_report
    reports = asyncio.run(go())
    failed = sum(1 for r in reports if not r.check.status.is_ok)
    unchanged = sum(1 for r in reports if r.check.not_modified)
    inited = sum(1 for r in reports if r.initialized)
    saved = sum(len(r.saved) for r in reports)
    errors = sum(len(r.errors) for r in reports)
    print(
        f"\n結果: 保存 {saved} 件 / 記事エラー {errors} 件"
        f"（フィード: 初回 {inited} / 更新なし {unchanged} / 取得失敗 {failed}）"
    )
    rc = 1 if errors else 0
    if not args.no_notify and conn.execute("SELECT 1 FROM discord_channels LIMIT 1").fetchone():
        rc |= _notify(cfg, conn)
        if cfg.discord.token:
            _sync_likes(cfg, conn)
    return rc


def _sync_likes(cfg: Config, conn: sqlite3.Connection, interval_hours: float = 6) -> int:
    try:
        n = notify.sync_likes(conn, _discord_client(cfg), interval_hours=interval_hours)
    except discord_api.DiscordError as e:
        # 権限不足などで取得できなくても、記事の取り込みと通知は成功しているので止めない
        print(f"👍 の取得に失敗: {e}（Bot に「メッセージ履歴を読む」権限があるか確認してください）", file=sys.stderr)
        return 1
    print(f"👍: {n} 件の投稿を確認")
    return 0


def cmd_likes(cfg: Config, args: argparse.Namespace) -> int:
    return _sync_likes(cfg, _open_db(cfg, args), interval_hours=0)


def cmd_interests(cfg: Config, args: argparse.Namespace) -> int:
    """投稿に付いた 👍 から、ジャンル・フィード別の興味の傾向を表示する。"""
    conn = _open_db(cfg, args)
    since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT f.category, f.title AS feed, i.title, i.likes FROM items i JOIN feeds f ON f.id = i.feed_id"
        " WHERE i.status = 'notified' AND i.notified_at >= ?",
        (since,),
    ).fetchall()
    if not rows:
        print(f"直近 {args.days} 日に投稿した記事がありません")
        return 0

    def table(key: str, label: str, limit: int | None = None) -> None:
        posts, liked = Counter(), Counter()
        for r in rows:
            posts[r[key] or "-"] += 1
            liked[r[key] or "-"] += bool(r["likes"])
        ranked = sorted(posts, key=lambda k: (-liked[k] / posts[k], -posts[k]))[:limit]
        print(f"\n{label}別（👍 が付いた投稿の割合）")
        for k in ranked:
            rate = liked[k] / posts[k]
            print(f"  {_fit(k, 30)} {'█' * round(rate * 20):<20} {rate:4.0%}  ({liked[k]}/{posts[k]})")

    total_liked = sum(1 for r in rows if r["likes"])
    print(f"直近 {args.days} 日: 投稿 {len(rows)} 件 / 👍 あり {total_liked} 件")
    table("category", "ジャンル")
    table("feed", "フィード", limit=args.top)
    liked_titles = [r for r in rows if r["likes"]][-args.top:]
    if liked_titles:
        print("\n最近 👍 した記事")
        for r in reversed(liked_titles):
            print(f"  [{r['category']}] {r['title']}")
    return 0


def cmd_summarize(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_db(cfg, args)

    async def go():
        async with Summarizer(cfg.summarize) as summarizer:
            return await summarize_pending(conn, summarizer, cfg.summarize.max_age_days, args.limit)

    done, total = asyncio.run(go())
    print(f"要約: {done} / {total} 件（公開から {cfg.summarize.max_age_days} 日以内・未通知の記事）")
    return 0 if done == total else 1


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


# --- discord ---------------------------------------------------------------

TOKEN_HELP = """Bot のトークンが設定されていません。
  1. https://discord.com/developers/applications で New Application → Bot → Reset Token
  2. .env に DISCORD_BOT_TOKEN=<トークン> を書く
  3. feedpipe discord invite-url で表示される URL から、Bot をサーバーに招待する"""


def _discord_client(cfg: Config) -> discord_api.DiscordClient:
    token = cfg.discord.token
    if not token:
        raise ValueError(TOKEN_HELP)
    return discord_api.DiscordClient(token)


def cmd_discord_invite_url(cfg: Config, args: argparse.Namespace) -> int:
    client = _discord_client(cfg)
    print(discord_api.invite_url(client.application_id()))
    print("権限: チャンネルを見る / チャンネルの管理 / ウェブフックの管理 / メッセージ履歴を読む", file=sys.stderr)
    return 0


def cmd_discord_guilds(cfg: Config, args: argparse.Namespace) -> int:
    for g in _discord_client(cfg).guilds():
        print(f"{g['id']}  {g['name']}")
    return 0


def cmd_discord_setup(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_db(cfg, args)
    client = _discord_client(cfg)
    guild = discord_api.resolve_guild(client, cfg.discord.guild_id)
    genre_defs = _load_genres(cfg)
    only = args.categories
    if only is None and genre_defs:
        # genres.toml があれば、そこで定義したトップレベルのジャンルだけフォーラムにする
        only = [g.path for g in genre_defs.values() if not g.is_sub]
    by_cat = discord_api.feeds_by_category(conn, only)
    plan = discord_api.make_plan(client, guild, by_cat, cfg.discord.parent_category, genre_defs)

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
            state = f"既存（{'、'.join(todo)}）" if todo else "既存"
        else:
            state = "作成"
        tags = "、".join(pc.wanted_tags())
        print(f"  {state:<10} 💬{pc.name:<16} ← {pc.category}（{len(pc.feeds)} フィード）")
        print(f"             タグ: {tags or '-'}")
        if pc.tag_overflow:
            print(f"             ※タグの上限 {discord_api.MAX_TAGS} 個を超える {pc.tag_overflow} フィードはタグなしで投稿します")
    for c in plan.orphans:
        print(f"  {'削除' if args.prune else '未使用'}      💬{c['name']}（どのジャンルにも対応しません）")
    if not args.apply:
        print("\n--apply を付けると作成します（既存のフォーラムはタグと Webhook だけ用意します。--prune で未使用を削除）")
        return 0
    created = discord_api.apply_plan(client, conn, plan, cfg.discord.webhook_name, nsfw=cfg.discord.nsfw)
    if args.prune:
        for c in plan.orphans:
            client.request("DELETE", f"/channels/{c['id']}")
        print(f"未使用のフォーラムを {len(plan.orphans)} 件削除しました")
    if genre_defs and not args.categories:
        # ジャンルに対応しなくなった送り先を消す（未割り当てのフィードが古いフォーラムに投稿されないように）
        names = [pc.category for pc in plan.channels]
        with conn:
            conn.execute(
                f"DELETE FROM discord_channels WHERE category NOT IN ({','.join('?' * len(names))})", names
            )
    print(f"\nフォーラムを {len(created)} 件作成し、{len(plan.channels)} 件の Webhook を登録しました")
    return 0


def cmd_discord_channels(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_db(cfg, args)
    rows = conn.execute("SELECT category, channel_name, channel_id FROM discord_channels ORDER BY category")
    for r in rows:
        print(f"{_fit(r['category'], 16)} #{r['channel_name']}  ({r['channel_id']})")
    return 0


def _notify(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    category: str | None = None,
    dry_run: bool = False,
) -> int:
    stats = notify.notify_pending(
        conn,
        limit=limit or cfg.discord.max_posts_per_run,
        max_age_days=cfg.discord.max_age_days,
        category=category,
        dry_run=dry_run,
    )
    line = f"通知: 送信 {stats.sent} 件"
    if stats.stale:
        line += f" / {cfg.discord.max_age_days} 日より古いため送らない {stats.stale} 件"
    if stats.failed:
        line += f" / 失敗 {stats.failed} 件"
    if stats.remaining:
        line += f" / 上限のため次回に回す {stats.remaining} 件"
    if stats.no_channel:
        line += f" / 送り先未登録 {stats.no_channel} 件（feedpipe discord setup）"
    print(line)
    for e in stats.errors:
        print(f"  ! {e}")
    return 1 if stats.failed else 0


def cmd_notify(cfg: Config, args: argparse.Namespace) -> int:
    return _notify(cfg, _open_db(cfg, args), limit=args.limit, category=args.category, dry_run=args.dry_run)


def cmd_discord_test(cfg: Config, args: argparse.Namespace) -> int:
    conn = _open_db(cfg, args)
    row = conn.execute("SELECT webhook_url, channel_name FROM discord_channels WHERE category = ?", (args.category,)).fetchone()
    if row is None:
        print(f"{args.category}: チャンネルが未登録です（feedpipe discord setup --apply）", file=sys.stderr)
        return 1
    # フォーラムへの投稿にはスレッド名が必要
    discord_api.send_webhook(
        row["webhook_url"], {"thread_name": "feedpipe テスト", "content": args.message, "username": "feedpipe"}
    )
    print(f"#{row['channel_name']} に送信しました")
    return 0


# --- genres ----------------------------------------------------------------


def _load_genres(cfg: Config) -> dict[str, genres.Genre] | None:
    path = Path(cfg.general.genres_file)
    return genres.load_genres(path) if path.exists() else None


def _require_genres(cfg: Config) -> dict[str, genres.Genre]:
    defs = _load_genres(cfg)
    if defs is None:
        raise ValueError(f"{cfg.general.genres_file} がありません（genres.example.toml を参考に作成してください）")
    return defs


def cmd_genres_list(cfg: Config, args: argparse.Namespace) -> int:
    defs = _require_genres(cfg)
    conn = _open_db(cfg, args)
    counts = Counter(f.category for f in db.list_feeds(conn, enabled=True))
    for g in defs.values():
        indent = "    " if g.is_sub else ""
        print(f"{indent}{_fit(g.path, 20 - len(indent))} {_fit(g.label, 14)} {counts[g.path]:>3} 件  {g.description}")
    return 0


def cmd_genres_apply(cfg: Config, args: argparse.Namespace) -> int:
    defs = _require_genres(cfg)
    conn = _open_db(cfg, args)
    r = genres.apply_genres(conn, defs, dry_run=args.dry_run, disable_unassigned=args.disable_unassigned)
    for feed, new in r.moved:
        print(f"  移動 {feed.id:>4} {_fit(feed.title or feed.url, 36)} {feed.category or '-'} → {new}")
    for f in r.unassigned:
        print(f"  未割り当て {f.id:>4} {_fit(f.title or f.url, 36)} {f.url}")
    print(
        f"移動 {len(r.moved)} 件 / 未割り当て {len(r.unassigned)} 件"
        f" / genres.toml にあるが未登録の URL {len(r.unknown_urls)} 件"
    )
    if args.dry_run:
        print("--dry-run のため DB は更新していません")
    elif r.moved:
        print("Discord のフォーラム・タグも更新するには: feedpipe discord setup --apply")
    if r.unassigned and args.disable_unassigned and not args.dry_run:
        print(f"未割り当ての {len(r.unassigned)} 件は無効にしました（feeds enable で戻せます）")
    elif r.unassigned:
        print("未割り当てのフィードの記事は投稿されません（--disable-unassigned で取得も止められます）")
    return 0


# --- bot -------------------------------------------------------------------


def cmd_bot(cfg: Config, args: argparse.Namespace) -> int:
    from .bot import run_bot  # discord.py の読み込みは bot のときだけ

    config_args = [*(["--config", str(args.config)] if args.config else []), *(["--db", args.db] if args.db else [])]
    run_bot(cfg, config_args)
    return 0


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

    gs = sub.add_parser("genres", help="ジャンル（genres.toml）の管理").add_subparsers(dest="genres_command", required=True)
    s = gs.add_parser("list", help="ジャンルの一覧とフィード数")
    s.set_defaults(func=cmd_genres_list)
    s = gs.add_parser("apply", help="genres.toml の割り当てをフィードに反映する")
    s.add_argument("--dry-run", action="store_true", help="DB を更新しない")
    s.add_argument("--disable-unassigned", action="store_true", help="genres.toml にないフィードを無効にする")
    s.set_defaults(func=cmd_genres_apply)

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
    s = dc.add_parser("test", help="カテゴリのチャンネルにテスト送信")
    s.add_argument("category")
    s.add_argument("message", nargs="?", default="feedpipe からのテスト送信です")
    s.set_defaults(func=cmd_discord_test)

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
    logging.getLogger("discord").setLevel(logging.INFO if args.verbose else logging.WARNING)
    # 音声機能は使わないので、PyNaCl などがないという警告は出さない
    logging.getLogger("discord.client").setLevel(logging.INFO if args.verbose else logging.ERROR)
    if args.command == "bot":
        # 常駐中は何をしたかを残したいので、-v がなくても feedpipe 自身のログは INFO で出す
        logging.getLogger("feedpipe").setLevel(logging.INFO)
    try:
        load_dotenv()
        cfg = load_config(args.config)
        return args.func(cfg, args)
    except (OSError, ValueError, ET.ParseError, discord_api.DiscordError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
