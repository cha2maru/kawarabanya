"""RSS 機能のコマンド（フィード管理・取り込み・要約・投稿・まとめ・👍）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from kawarabanya.cli.common import (
    STATUS_MARK,
    _discord_client,
    _fit,
    _load_genres,
    _open_db,
    _require_genres,
)
from kawarabanya.core import db, discord_api
from kawarabanya.core.config import Config
from kawarabanya.core.llm import Summarizer
from kawarabanya.rss import forums, genres, notify, prefs
from kawarabanya.rss.pipeline import FeedReport, run_feeds, summarize_pending
from kawarabanya.rss.sources import opml
from kawarabanya.rss.sources.check import (
    CheckResult,
    Status,
    apply_results,
    check_feeds,
)


def cmd_import_opml(cfg: Config, args: argparse.Namespace) -> int:
    """import-opml: OPML のフィードを登録し、genres.toml のジャンルにも書き足す。

    ジャンルは --genre、なければ OPML のフォルダ名と同じジャンル（"tech" や "tech/ai"、"tech/dev" なら tech）。
    --genre auto なら、フォルダ名で決まらないフィードを LLM が決める（フィードの最近の見出しから）。
    どれにも当たらないフィードは登録だけする（genres.toml に書くまで投稿されない）。
    """
    from kawarabanya.rss import placement

    parsed = opml.parse_opml(args.file)
    defs = _load_genres(cfg)
    auto = args.genre == placement.AUTO
    if args.genre and not auto and (defs is None or args.genre not in defs):
        print(f"{args.genre}: genres.toml にないジャンルです", file=sys.stderr)
        return 1
    conn = _open_db(cfg, args)
    stats = opml.import_entries(conn, parsed.entries, update=args.update)
    print(f"{args.file}: フィード {len(parsed.entries)} 件（重複 {len(parsed.duplicates)} 件は除外）")
    print(f"  追加 {stats.added} / 更新 {stats.updated} / 既存のためスキップ {stats.skipped}")
    if args.verbose:
        for url, category in parsed.duplicates:
            print(f"  重複: [{category}] {url}")
    if defs is None or not stats.new:
        return 0
    written = {u for g in defs.values() for u in g.feeds}
    chosen = {
        e.url: (None if auto else args.genre)
        or next((c for c in (e.category, genres.top_genre(e.category)) if c and c in defs), None)
        for e in stats.new
    }
    if auto and (todo := [e for e in stats.new if chosen[e.url] is None and e.url not in written]):
        print(f"  ジャンルを LLM で決めます: {len(todo)} 件", flush=True)

        async def guess():
            async with Summarizer(cfg.summarize) as s:
                for e in todo:
                    titles = await placement.recent_titles(e.url, cfg.general)
                    chosen[e.url] = await placement.guess_genre(s, defs, e.title, titles)

        asyncio.run(guess())
    placed: dict[str, int] = {}
    unplaced = 0
    for e in stats.new:
        genre = chosen[e.url]
        if genre is None or e.url in written:
            unplaced += genre is None
            continue
        genres.add_feed_url(cfg.general.genres_file, genre, e.url, e.title)
        with conn:
            conn.execute("UPDATE feeds SET category = ? WHERE url = ?", (genre, e.url))
        placed[genre] = placed.get(genre, 0) + 1
    if placed:
        print("  genres.toml に書き足しました: " + "、".join(f"{g} {n} 件" for g, n in placed.items()))
    if unplaced:
        print(f"  ジャンルの決まらない {unplaced} 件は、genres.toml に書くまで投稿されません（--genre で指定・auto で LLM）")
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
    db.set_state(conn, "run_at", datetime.now(UTC).isoformat(timespec="seconds"))
    rc = 0  # 記事単位の失敗は上の結果に出し、run の失敗にはしない（Bot が不調として数えないように）
    if not args.no_notify and conn.execute("SELECT 1 FROM discord_channels LIMIT 1").fetchone():
        rc |= _notify(cfg, conn)
        if cfg.discord.token:
            _sync_likes(cfg, conn)
    if cfg.digest.enabled and cfg.digest.trends_geo:
        _fetch_trends(cfg, conn)
    return rc


def _fetch_trends(cfg: Config, conn: sqlite3.Connection) -> None:
    """Google トレンドの急上昇ワードをためる。取れなくても run は止めない（まとめの加点がなくなるだけ）。"""
    from xml.etree.ElementTree import ParseError

    from kawarabanya.rss import trends

    try:
        n = trends.fetch(conn, cfg.digest.trends_geo, cfg.general.user_agent, cfg.general.timeout)
    except (httpx.HTTPError, ParseError) as e:
        print(f"Google トレンドの取得に失敗: {e}", file=sys.stderr)
        return
    print(f"Google トレンド: {n} 語（直近 24 時間）")

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
    _print_prefs(conn, prefs.learn(conn, datetime.now(UTC), cfg.digest.pref_half_life_days), args.top)
    return 0


def _print_prefs(conn: sqlite3.Connection, learned: prefs.Prefs, top: int) -> None:
    """👍・👎 から学んだ好み（まとめの並べ替えに使う w）の上位と下位を表示する。"""
    if not learned.words and not learned.sites:
        print("\n学んだ好み: まだありません（👍・👎 が付くとたまります）")
        return
    names = dict(conn.execute("SELECT id, title FROM feeds").fetchall())
    for label, ws in [("ジャンル・語", learned.words), ("サイト", {names.get(k, k): v for k, v in learned.sites.items()})]:
        ranked = sorted(ws.items(), key=lambda kv: -kv[1])
        print(f"\n学んだ好み（{label}。+ は 👍 寄り、- は 👎 寄り）")
        for k, v in [*ranked[:top], *(x for x in ranked[-top:] if x[1] < 0 and x not in ranked[:top])]:
            print(f"  {_fit(str(k), 30)} {v:+.2f}")

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
    """feeds add: サイトかフィードの URL からフィードを探して登録し、genres.toml の --genre にも書き足す。

    --genre auto なら、フィードの最近の見出しから LLM がジャンルを決める。
    """
    from kawarabanya.rss import placement
    from kawarabanya.rss.sources.discover import discover

    conn = _open_db(cfg, args)
    defs = _load_genres(cfg)
    if args.genre and args.genre != placement.AUTO and defs is not None and args.genre not in defs:
        print(f"{args.genre}: genres.toml にないジャンルです（{', '.join(g for g in defs if '/' not in g)}）", file=sys.stderr)
        return 1
    url, title = args.url, args.title
    if not args.no_discover:
        found = asyncio.run(discover(args.url, cfg.general))
        if found is None:
            print(f"{args.url}: フィードが見つかりません", file=sys.stderr)
            try:
                hits = [r for r in _rsshub_routes(cfg, args.url) if r.matched]
            except httpx.HTTPError:
                hits = []
            for r in hits[:3]:
                print(f"  RSSHub なら {r.route}（{r.title}）で取れます", file=sys.stderr)
            print("  ほかの手: genres.toml に HTML 取り込みか、extension = \"sitemap\"（サイトマップ）を書く", file=sys.stderr)
            return 1
        url, title = found[0], title or found[1] or None
    existing = db.find_feed_by_url(conn, url)
    if existing and existing.enabled:
        print(f"登録済みです: {existing.id} {existing.title or ''} {existing.url}（{existing.category or '-'}）", file=sys.stderr)
        return 1
    if args.genre == placement.AUTO:
        args.genre = None
        if defs is not None:

            async def guess():
                async with Summarizer(cfg.summarize) as s:
                    return await placement.guess_genre(s, defs, title, await placement.recent_titles(url, cfg.general))

            args.genre = asyncio.run(guess())
            print(f"LLM が選んだジャンル: {args.genre or '決められませんでした（ジャンルなしで登録）'}")
    with conn:
        if existing:  # feeds remove で外したサイト → 有効に戻す（記事の既読も残っている）
            feed_id = existing.id
            db.set_enabled(conn, feed_id, True)
            conn.execute("UPDATE feeds SET category = ? WHERE id = ?", (args.genre, feed_id))
        else:
            feed_id = db.insert_feed(conn, url, title, None, args.genre)
    # genres.toml に同じ URL を 2 回書くと読めなくなる（Bot も止まる）ので、書いてあれば足さない
    written = {u for g in (defs or {}).values() for u in g.feeds}
    if args.genre and defs is not None and url not in written:
        genres.add_feed_url(cfg.general.genres_file, args.genre, url, title)
    print(f"{'有効に戻しました' if existing else '追加しました'}: {feed_id} {title or ''} {url}（{args.genre or 'ジャンルなし'}）")
    if not existing:
        print("次の取り込みでは既存の記事を既読にするだけで、それ以降の新着から投稿します")
    return 0


def _rsshub_routes(cfg: Config, url: str) -> list:
    from kawarabanya.rss.sources import rsshub

    cache = Path(cfg.general.db).parent / "rsshub-radar.json"
    return rsshub.match(url, rsshub.load_rules(cache, cfg.general.user_agent))


def cmd_feeds_rsshub(cfg: Config, args: argparse.Namespace) -> int:
    """feeds rsshub: URL が RSSHub のどのルートで取れるかを調べる（RSSHub の radar ルール）。"""
    routes = _rsshub_routes(cfg, args.url)
    hits = [r for r in routes if r.matched]
    if hits:
        print(f"RSSHub で取れます（{len(hits)} 件）:")
    elif routes:
        print("このサイトのルートはありますが、この URL のパスには当たりません:")
    else:
        print("RSSHub のルートはありません")
    for r in (hits or routes)[:10]:
        print(f"  {r.route}  {r.title}  {r.docs}")
    if hits:
        base = cfg.general.rsshub_url or "http://127.0.0.1:1200"
        print(f"RSSHub を動かして {base}<ルート> を feeds add で登録する（名前もそのとき付く）")
    return 0


def cmd_feeds_remove(cfg: Config, args: argparse.Namespace) -> int:
    """feeds remove: フィードを無効にし、genres.toml から外す（保存済みの記事と投稿は残す）。"""
    conn = _open_db(cfg, args)
    feed = db.get_feed(conn, args.id) if args.id.isdigit() else db.find_feed_by_url(conn, args.id)
    if feed is None:
        print(f"{args.id}: 見つかりません", file=sys.stderr)
        return 1
    with conn:
        db.set_enabled(conn, feed.id, False, "removed")
    removed = 0
    if _load_genres(cfg) is not None:
        removed = genres.remove_feed_urls(cfg.general.genres_file, [feed.url, feed.original_url])
    print(f"外しました: {feed.id} {feed.title or ''} {feed.url}（genres.toml から {removed} 行）")
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
        levels=genres.interest_levels(_load_genres(cfg)),
        dry_run=dry_run,
    )
    line = f"通知: 送信 {stats.sent} 件"
    if stats.muted:
        line += f" / ミュートのため送らない {stats.muted} 件"
    if stats.stale:
        line += f" / {cfg.discord.max_age_days} 日より古いため送らない {stats.stale} 件"
    if stats.failed:
        line += f" / 失敗 {stats.failed} 件"
    if stats.remaining:
        line += f" / 上限のため次回に回す {stats.remaining} 件"
    if stats.no_channel:
        line += f" / 送り先未登録 {stats.no_channel} 件（kawarabanya discord setup）"
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
        print("Discord のフォーラム・タグも更新するには: kawarabanya discord setup --apply")
    if r.unassigned and args.disable_unassigned and not args.dry_run:
        print(f"未割り当ての {len(r.unassigned)} 件は無効にしました（feeds enable で戻せます）")
    elif r.unassigned:
        print("未割り当てのフィードの記事は投稿されません（--disable-unassigned で取得も止められます）")
    return 0

def cmd_genres_interest(cfg: Config, args: argparse.Namespace) -> int:
    """genres interest: ジャンル・サブジャンルの好き嫌いを genres.toml に書く。"""
    defs = _require_genres(cfg)
    if args.genre not in defs:
        print(f"{args.genre}: genres.toml にありません", file=sys.stderr)
        return 1
    genres.set_interest(cfg.general.genres_file, args.genre, args.level)
    print(f"{args.genre}（{defs[args.genre].label}）: {genres.INTEREST_LEVELS[args.level]}")
    return 0


def cmd_genres_keyword(cfg: Config, args: argparse.Namespace) -> int:
    """genres keyword: 振り分けの候補を絞る語を genres.toml に書く。"""
    defs = _require_genres(cfg)
    if args.genre not in defs:
        print(f"{args.genre}: genres.toml にありません", file=sys.stderr)
        return 1
    genres.add_keyword(cfg.general.genres_file, args.genre, args.word)
    print(f"{args.word} → {args.genre}（{defs[args.genre].label}）")
    return 0


def cmd_items_move(cfg: Config, args: argparse.Namespace) -> int:
    """items move: 記事を別のジャンルに移す（--keyword があれば語も登録する）。"""
    defs = _require_genres(cfg)
    if args.genre not in defs:
        print(f"{args.genre}: genres.toml にありません", file=sys.stderr)
        return 1
    if args.keyword:
        genres.add_keyword(cfg.general.genres_file, args.genre, args.keyword)
    print(notify.move_post(_open_db(cfg, args), _discord_client(cfg), args.id, args.genre))
    return 0


def cmd_items_translate(cfg: Config, args: argparse.Namespace) -> int:
    """items translate: 日本語以外の記事の全文を訳し、保存して投稿のスレッドに返信する。"""
    conn = _open_db(cfg, args)
    client = _discord_client(cfg) if cfg.discord.token else None

    async def go():
        async with Summarizer(cfg.summarize) as summarizer:
            return await notify.translate_post(conn, client, summarizer, args.id)

    print(asyncio.run(go()))
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    """status: 最後の取り込み・次のまとめ・要約サーバー・フィードと記事の状態を表示する。"""
    import httpx

    from kawarabanya.rss import digest, suggest

    conn = _open_db(cfg, args)
    now = datetime.now(UTC)

    def local(iso: str | None) -> str:
        return datetime.fromisoformat(iso).astimezone().strftime("%m/%d %H:%M") if iso else "-"

    print(f"最後の取り込み: {local(db.get_state(conn, 'run_at'))}")
    if cfg.digest.enabled and (nxt := digest.next_slot(now, cfg.digest.times)):
        print(f"次のまとめ: {nxt:%m/%d %H:%M}")
    try:
        ok = httpx.get(cfg.summarize.endpoint.rstrip("/").removesuffix("/v1") + "/health", timeout=3).is_success
    except httpx.HTTPError:
        ok = False
    print(f"要約サーバー: {'動いています' if ok else '応答がありません'}")
    feeds = db.list_feeds(conn, enabled=True)
    failing = [f for f in feeds if f.fail_count]
    print(f"フィード: 有効 {len(feeds)} 件 / 取得に失敗中 {len(failing)} 件")
    for f in failing[:5]:
        print(f"  - {f.id} {f.title or f.url}（{f.fail_count} 回: {f.last_status}）")
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
    print(f"記事: 投稿待ち {counts.get('saved', 0)} 件 / 投稿済み {counts.get('notified', 0)} 件"
          f" / 重複 {counts.get('duplicate', 0)} 件 / ミュート {counts.get('muted', 0)} 件")
    if (defs := _load_genres(cfg)) is not None:
        print(f"どのサブジャンルにも当てはまらない記事（7 日）: {len(suggest.unmatched_items(conn, defs, 7))} 件")
    return 0


def cmd_genres_suggest(cfg: Config, args: argparse.Namespace) -> int:
    """genres suggest: 当てはまるサブジャンルのなかった記事から、新しいサブジャンルを LLM に決めさせる。

    タグの上限（20）を超えるジャンルは、似たサブジャンルを先にまとめる。
    --apply で genres.toml に反映し、記事のサブジャンルを書き換え、Discord のタグを揃えて付け直す。
    --due は Bot の定期実行用（まとめの時刻 [digest] times を過ぎて、まだ実行していなければ実行する）。
    """
    from kawarabanya.rss import digest, suggest

    defs = _require_genres(cfg)
    conn = _open_db(cfg, args)
    now = datetime.now(UTC)
    if args.due:
        slot = digest.last_slot(now, cfg.digest.times)
        last = db.get_state(conn, "suggest_at")
        if slot is None or (last and datetime.fromisoformat(last) >= slot):
            return 0
        db.set_state(conn, "suggest_at", now.isoformat(timespec="seconds"))
    rows = suggest.unmatched_items(conn, defs, args.days)
    print(f"どのサブジャンルにも当てはまらなかった記事: {len(rows)} 件（直近 {args.days} 日）", flush=True)

    async def go():
        async with Summarizer(cfg.summarize) as summarizer:
            found = await suggest.suggest(conn, summarizer, defs, args.days)
            return await suggest.plan_merges(summarizer, defs, found)

    found, merges = asyncio.run(go())
    titles = {r["id"]: r["title"] for r in rows}
    for s in found:
        print(f"  [{s.path}] {s.label}（{s.description}）: {len(s.item_ids)} 件")
        for i in s.item_ids:
            print(f"      - {titles.get(i)}")
    for mg in merges:
        names = "、".join(defs[f"{mg.genre}/{k}"].label if f"{mg.genre}/{k}" in defs else k for k in mg.keys)
        print(f"  統合 [{mg.genre}] {names} → {mg.label}（{mg.description}）")
    print(f"新しいサブジャンル: {len(found)} 件 / 統合: {len(merges)} 件")
    if not found or not args.apply:
        if found:
            print("--apply で genres.toml に反映します")
        return 0
    removed = [r for mg in merges for r in mg.removed]
    merged_ids = [r[0] for r in conn.execute(
        f"SELECT id FROM items WHERE subgenre IN ({','.join('?' * len(removed))})", removed
    )] if removed else []
    suggest.apply(conn, cfg.general.genres_file, found, merges)
    print(f"genres.toml に反映しました: {', '.join(s.path for s in found)}")
    if not (cfg.discord.token and conn.execute("SELECT 1 FROM discord_channels LIMIT 1").fetchone()):
        return 0
    defs = _load_genres(cfg)
    client = _discord_client(cfg)
    guild = discord_api.resolve_guild(client, cfg.discord.guild_id)
    only = [g.path for g in defs.values() if not g.is_sub]
    plan = forums.make_plan(
        client, guild, forums.feeds_by_category(conn, only), cfg.discord.parent_category, defs
    )
    forums.apply_plan(client, conn, plan, cfg.discord.webhook_name, nsfw=cfg.discord.nsfw)
    ids = [i for s in found for i in s.item_ids] + merged_ids
    changed, _ = asyncio.run(notify.retag_posts(conn, client, None, defs, ids))
    print(f"Discord のタグを作り、投稿済みの {changed} 件のタグを付け直しました")
    return 0


def cmd_digest(cfg: Config, args: argparse.Namespace) -> int:
    """digest: ジャンルごとのまとめ（前回のまとめ以降の記事）を投稿する（--update で書き換え、--delete で削除）。"""
    from kawarabanya.rss import digest

    conn = _open_db(cfg, args)
    now = datetime.now(UTC)

    if args.delete:
        if args.day:  # その日のまとめをすべて
            rows = conn.execute("SELECT genre, day FROM digests WHERE day LIKE ?", (f"{args.day}%",)).fetchall()
        else:  # ジャンルごとに最後のまとめ
            rows = conn.execute(
                "SELECT genre, day FROM digests d WHERE created_at = (SELECT MAX(created_at) FROM digests WHERE genre = d.genre)"
            ).fetchall()
        deleted = [r["genre"] for r in rows if (not args.genre or r["genre"] in args.genre)
                   and digest.delete_digest(conn, r["genre"], r["day"])]
        print(f"まとめ: {len(deleted)} 件を削除しました（{', '.join(deleted) or 'なし'}）")
        return 0
    if args.day:
        raise ValueError("--day は --delete と一緒に使います")
    due_after = None
    if args.due:
        due_after = digest.last_slot(now, cfg.digest.times)
        if not cfg.digest.enabled or due_after is None:
            return 0

    defs = _load_genres(cfg)
    genre_list = args.genre or (
        [g.path for g in defs.values() if not g.is_sub] if defs
        else [r[0] for r in conn.execute(
            "SELECT category FROM discord_channels WHERE category NOT IN (?, ?) ORDER BY category",
            (discord_api.DIGEST_KEY, discord_api.SCHEDULE_KEY),
        )]
    )

    levels = genres.interest_levels(defs)

    d = cfg.digest
    learned = prefs.learn(conn, now, d.pref_half_life_days)

    async def go():
        async with Summarizer(cfg.summarize) as summarizer:
            return {
                g: await digest.post_digest(
                    conn, summarizer, g, label=defs[g].label if defs and g in defs else None,
                    levels=levels, now=now, top=cfg.digest.top, window_hours=cfg.digest.window_hours,
                    due_after=due_after, learned=learned, pref_max=d.pref_max, site_max=d.site_pref_max,
                    update=args.update, dry_run=args.dry_run,
                )
                for g in genre_list
            }

    results = asyncio.run(go())
    done = [g for g, c in results.items() if c]
    if args.dry_run:
        for g in done:
            print(f"===== {g} =====\n{results[g]}\n")
    elif done and not args.update:
        digest.announce(conn, [defs[g].label if defs and g in defs else g for g in done], cfg.discord.mention_user_id)
    if done or not args.due:
        verb = "表示" if args.dry_run else ("更新" if args.update else "投稿")
        print(f"まとめ: {verb} {len(done)} 件（{', '.join(done) or 'なし'}）")
    return 0
