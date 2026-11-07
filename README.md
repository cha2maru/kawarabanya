# feedpipe

個人用 Discord サーバーを管理する Bot。今ある機能は RSS: 記事を全文 Markdown で保存し、ローカル LLM で要約して、ジャンルごとの Discord フォーラムに投稿する。設計は [DESIGN.md](DESIGN.md)。

## セットアップ

```sh
uv sync

# 1. Discord Bot（https://discord.com/developers/applications で作成）
cp .env.example .env                         # DISCORD_BOT_TOKEN を書く
uv run feedpipe discord invite-url           # この URL からサーバーに招待する（サーバーは「コミュニティ」を有効に）

# 2. フィードとジャンル
uv run feedpipe import-opml feedly.opml
uv run feedpipe check --disable-dead         # つながらないフィードを無効化
cp genres.example.toml genres.toml           # ジャンルとフィードの URL を書く
uv run feedpipe genres apply --disable-unassigned

# 3. Discord のフォーラム（ジャンルごと）を作る
uv run feedpipe discord setup                # 予定を表示
uv run feedpipe discord setup --apply

# 4. 要約用のモデル（llama.cpp と Qwen3.5-4B）を用意し、scripts/llama-server.sh のパスを合わせる

# 5. 常駐 Bot をサービスとして起動
cp scripts/feedpipe-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now feedpipe-bot
journalctl --user -u feedpipe-bot -f         # ログ
```

## コマンド

| コマンド | 内容 |
|---|---|
| `bot` | 常駐 Bot。60 分ごとに `run` を別プロセスで実行する |
| `run [--category X] [--backfill N] [--no-fulltext] [--no-summary] [--no-notify]` | 取得 → 保存 → 要約 → 投稿 → 👍 集計。初回のフィードは既存記事を既読にするだけ |
| `summarize` | 保存済み・未投稿で要約のない最近の記事を要約する |
| `notify [--category X] [--limit N] [--dry-run]` | 保存済み・未投稿の記事を投稿する |
| `likes` / `interests [--days 30]` | 👍 の数を取得する / ジャンル・フィード別の 👍 の割合を表示する |
| `import-opml FILE` / `export-opml FILE [--all]` | OPML の取り込み・書き出し |
| `check [--disable-dead]` | 死活チェック（移転していれば URL を更新） |
| `feeds list\|add\|enable\|disable\|option` | フィードの管理（例: `feeds option ID no_fulltext true`） |
| `genres list` / `genres apply [--dry-run] [--disable-unassigned]` | ジャンルの一覧 / genres.toml の反映 |
| `discord setup [--apply] [--prune]` / `channels` / `test GENRE` / `guilds` / `invite-url` | フォーラム・タグ・Webhook の管理 |

全体のオプション: `-c config.toml`、`--db PATH`、`-v`（詳しいログ）。

## 設定

### genres.toml（git 管理外。見本: genres.example.toml）

```toml
[tech]                       # ジャンル = Discord のフォーラム 1 つ
label = "テック"
description = "IT・ガジェット・開発"
feeds = [
  "https://gigazine.net/news/rss_2.0/",  # GIGAZINE
]

[hobby.boardgame]            # サブジャンル = フォーラムのタグ
label = "ボードゲーム"
feeds = [ ... ]
```

フィードを移すには URL の行を別のジャンルに移して `genres apply` → `discord setup --apply`。書かれていないフィードの記事は投稿されない（保存はする）。

### config.toml（git 管理外。すべて省略可）

```toml
[general]
db = "data/feedpipe.db"
archive_dir = "archive"
max_failures = 5             # 連続失敗でフィードを無効化する回数

[fulltext]
min_length = 200             # 元記事の本文がこれより短ければフィードの本文と長い方を使う

[summarize]
endpoint = "http://127.0.0.1:8080/v1"
model = "Qwen3.5-4B"
max_age_days = 3             # 公開からこの日数以内の記事だけ要約する

[discord]
nsfw = true                  # フォーラムを年齢制限にする
max_age_days = 3             # これより古い記事は投稿しない
max_posts_per_run = 30

[bot]
run_interval_minutes = 60    # 0 なら定期実行しない
run_args = []                # run に渡す追加の引数（例: ["--no-summary"]）
llm_server_command = "scripts/llama-server.sh"   # Bot と一緒に要約サーバーを起動・停止する
```

## 開発

```sh
uv run pytest
uvx ruff check src tests
```

対話機能（スラッシュコマンドなど）は `src/feedpipe/bot.py` の `Bot` に足していく。
