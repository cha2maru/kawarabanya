"""RSS 機能のコマンド（フィード管理・取り込み・要約・投稿・まとめ・👍）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

from .. import db, discord_api
from ..config import Config
from ..llm import Summarizer
from ..rss import genres, notify, opml
from ..rss.check import CheckResult, Status, apply_results, check_feeds
from ..rss.pipeline import FeedReport, run_feeds, summarize_pending
from .common import (
    STATUS_MARK,
    _discord_client,
    _fit,
    _load_genres,
    _open_db,
    _require_genres,
)


def cmd_import_opml(cfg: Config, args: argparse.Namespace) -> int:
    """import-opml: OPML のフィードを登録する。"""
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
    """export-opml: 登録済みのフィードを OPML に書き出す。"""
    conn = _open_db(cfg, args)
    feeds = db.list_feeds(conn, enabled=None if args.all else True)
    opml.export_opml(feeds, args.file)
    print(f"{args.file}: {len(feeds)} 件を書き出しました")
    return 0

def _print_result(r: CheckResult) -> None:
    """死活チェックの結果を 1 行で表示する。"""
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
    """check: フィードの死活チェックをして、結果を DB に反映する。"""
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

def _select_feeds(conn: sqlite3.Connection, args: argparse.Namespace) -> list[db.Feed]:
    """--id か --category で指定された有効なフィードを返す。"""
    if args.id:
        return [f for i in args.id if (f := db.get_feed(conn, i))]
    return db.list_feeds(conn, enabled=True, category=args.category)

def _print_report(r: FeedReport) -> None:
    """フィード 1 件分の取り込み結果を表示する。"""
    title = _fit(r.feed.title or r.feed.url, 36)
    if not r.check.status.is_ok:
        print(f"[{STATUS_MARK[r.check.status]}] {r.feed.id:>4} {title} {r.check.error}", flush=True)
        return
    if r.initialized:
        print(f"[INIT] {r.feed.id:>4} {title} 既存 {r.check.entries} 件を既読化"
              + (f"（うち {len(r.saved)} 件を保存）" if r.saved else ""), flush=True)
    elif r.new:
        print(f"[NEW ] {r.feed.id:>4} {title} 新着 {r.new} 件 / 保存 {len(r.saved)} 件"
              + (f" / 重複 {r.duplicates} 件" if r.duplicates else ""), flush=True)
    for it in r.saved:
        print(f"         + [{it.content_source}{' 要約' if it.summary else ''}] {it.title}")
    for it, err in r.errors:
        print(f"         ! {it.title}: {err}")

def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    """run: 新着記事を取り込み（保存・判定・要約）、投稿して 👍 を集計する。"""
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
    dups = sum(r.duplicates for r in reports)
    print(
        f"\n結果: 保存 {saved} 件 / 重複 {dups} 件 / 記事エラー {errors} 件"
        f"（フィード: 初回 {inited} / 更新なし {unchanged} / 取得失敗 {failed}）"
    )
    rc = 1 if errors else 0
    if not args.no_notify and conn.execute("SELECT 1 FROM discord_channels LIMIT 1").fetchone():
        rc |= _notify(cfg, conn)
        if cfg.discord.token:
            _sync_likes(cfg, conn)
    return rc

def _sync_likes(cfg: Config, conn: sqlite3.Connection, interval_hours: float = 6) -> int:
    """投稿の 👍 の数を取得する。取得に失敗しても run は止めない。"""
    try:
        n = notify.sync_likes(conn, _discord_client(cfg), interval_hours=interval_hours)
    except discord_api.DiscordError as e:
        # 権限不足などで取得できなくても、記事の取り込みと通知は成功しているので止めない
        print(f"👍 の取得に失敗: {e}（Bot に「メッセージ履歴を読む」権限があるか確認してください）", file=sys.stderr)
        return 1
    print(f"👍: {n} 件の投稿を確認")
    return 0

def cmd_likes(cfg: Config, args: argparse.Namespace) -> int:
    """likes: 投稿の 👍 の数を、間隔を空けずに今すぐ取得する。"""
    return _sync_likes(cfg, _open_db(cfg, args), interval_hours=0)

def cmd_interests(cfg: Config, args: argparse.Namespace) -> int:
    """投稿に付いた 👍 から、ジャンル・フィード別の興味の傾向を表示する。"""
    conn = _open_db(cfg, args)
    since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT f.category, f.title AS feed, i.title, i.likes, i.discord_thread_id FROM items i"
        " JOIN feeds f ON f.id = i.feed_id"
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
        guild = conn.execute("SELECT guild_id FROM discord_channels LIMIT 1").fetchone()
        print("\n最近 👍 した記事")
        for r in reversed(liked_titles):
            print(f"  [{r['category']}] {r['title']}")
            if guild and r["discord_thread_id"]:
                print(f"      {discord_api.post_url(guild[0], r['discord_thread_id'])}")
    return 0

def cmd_summarize(cfg: Config, args: argparse.Namespace) -> int:
    """summarize: 保存済み・未投稿で要約のない最近の記事を、重要度を判定してから要約する。"""
    conn = _open_db(cfg, args)

    async def go():
        async with Summarizer(cfg.summarize) as summarizer:
            return await summarize_pending(conn, summarizer, cfg, args.limit)

    r = asyncio.run(go())
    print(
        f"要約 {r['summarized']} 件 / 重要度が基準（{cfg.summarize.priority_threshold}）未満のため要約しない {r['low']} 件"
        f" / 失敗 {r['failed']} 件（公開から {cfg.summarize.max_age_days} 日以内・未通知の記事）"
    )
    return 1 if r["failed"] else 0

def cmd_feeds_list(cfg: Config, args: argparse.Namespace) -> int:
    """feeds list: フィードの一覧を表示する。"""
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
    """feeds add: フィードを 1 件登録する。"""
    conn = _open_db(cfg, args)
    if existing := db.find_feed_by_url(conn, args.url):
        print(f"登録済みです: {existing.id} {existing.url}", file=sys.stderr)
        return 1
    with conn:
        feed_id = db.insert_feed(conn, args.url, args.title, None, args.category)
    print(f"追加しました: {feed_id} {args.url}")
    return 0

def cmd_feeds_option(cfg: Config, args: argparse.Namespace) -> int:
    """feeds option: フィード個別の設定を変える（値は JSON として解釈し、だめなら文字列）。"""
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
    """feeds enable / disable: フィードを有効・無効にする。"""
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

def _notify(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    category: str | None = None,
    dry_run: bool = False,
) -> int:
    """保存済み・未投稿の記事を投稿し、結果を表示する。失敗があれば 1 を返す。"""
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
    """notify: 保存済み・未投稿の記事を投稿する。"""
    return _notify(cfg, _open_db(cfg, args), limit=args.limit, category=args.category, dry_run=args.dry_run)

def cmd_genres_list(cfg: Config, args: argparse.Namespace) -> int:
    """genres list: ジャンルと、それぞれの有効なフィード数を表示する。"""
    defs = _require_genres(cfg)
    conn = _open_db(cfg, args)
    counts = Counter(f.category for f in db.list_feeds(conn, enabled=True))
    for g in defs.values():
        indent = "    " if g.is_sub else ""
        print(f"{indent}{_fit(g.path, 20 - len(indent))} {_fit(g.label, 14)} {counts[g.path]:>3} 件  {g.description}")
    return 0

def cmd_genres_apply(cfg: Config, args: argparse.Namespace) -> int:
    """genres apply: genres.toml の割り当てをフィードの category に反映する。"""
    defs = _require_genres(cfg)
    conn = _open_db(cfg, args)
    r = genres.apply_genres(conn, defs, dry_run=args.dry_run, disable_unassigned=args.disable_unassigned)
    for feed, new in r.moved:
        print(f"  移動 {feed.id:>4} {_fit(feed.title or feed.url, 36)} {feed.category or '-'} → {new}")
    for url in r.added:
        print(f"  追加（HTML 取り込み） {url}")
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

def cmd_digest(cfg: Config, args: argparse.Namespace) -> int:
    """digest: ジャンルごとの今日のまとめを投稿する（--update で書き換え、--delete で削除）。"""
    from ..rss import digest

    conn = _open_db(cfg, args)
    now = datetime.now(UTC)

    if args.delete:
        day = args.day or now.astimezone().strftime("%Y-%m-%d")
        targets = args.genre or [r[0] for r in conn.execute("SELECT genre FROM digests WHERE day = ?", (day,))]
        deleted = [g for g in targets if digest.delete_digest(conn, g, day)]
        print(f"まとめ: {day} の {len(deleted)} 件を削除しました（{', '.join(deleted) or 'なし'}）")
        return 0
    if args.day:
        raise ValueError("--day は --delete と一緒に使います（投稿・更新は今日のまとめだけ）")
    if args.due and not (cfg.digest.enabled and digest.is_due(now, cfg.digest.hour)):
        return 0

    defs = _load_genres(cfg)
    genre_list = args.genre or (
        [g.path for g in defs.values() if not g.is_sub] if defs
        else [r[0] for r in conn.execute(
            "SELECT category FROM discord_channels WHERE category != ? ORDER BY category", (discord_api.DIGEST_KEY,)
        )]
    )

    async def go():
        async with Summarizer(cfg.summarize) as summarizer:
            return {
                g: await digest.post_digest(
                    conn, summarizer, g, label=defs[g].label if defs and g in defs else None, now=now, top=cfg.digest.top, window_hours=cfg.digest.window_hours,
                    update=args.update, dry_run=args.dry_run,
                )
                for g in genre_list
            }

    results = asyncio.run(go())
    done = [g for g, c in results.items() if c]
    if args.dry_run:
        for g in done:
            print(f"===== {g} =====\n{results[g]}\n")
    if done or not args.due:
        verb = "表示" if args.dry_run else ("更新" if args.update else "投稿")
        print(f"まとめ: {verb} {len(done)} 件（{', '.join(done) or 'なし'}）")
    return 0
