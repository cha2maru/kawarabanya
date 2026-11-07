# RSS アーカイブ & 通知パイプライン 設計検討（v0.1 草案）

最終更新: 2026-11-07

## 1. 目的と方針

- RSS/Atom（将来は RSS 以外も）を定期取得し、**記事全文を Markdown で保存**、**Discord に通知**する。
- 必要に応じて**ローカル LLM（CPU）で日本語要約**を付ける。
- **方針: 軽さ優先**。常駐サーバー・DB サーバー・Docker を必須にしない。単一の CLI + SQLite + 設定ファイルで完結させる。
- **OPML 取り込みは必須機能**（既存の `feedly.opml` を初期データとする）。
- 構造は Plagger 風に **Source → Filter → Output** のプラグイン構成。

## 2. 既存データ（feedly.opml）の調査結果

| 項目 | 値 |
|---|---|
| フィード数（xmlUrl あり） | 338 件（重複除外後 333 件） |
| カテゴリ（フォルダ） | 15 個（tech, news, game, 2ch, ホビー …） |
| `http://` のフィード | 291 件（大半が古い URL） |
| 明らかに終了したサービス | Yahoo! Pipes（3）、Yahoo!ブログ（3）、はてなダイアリー d.hatena.ne.jp（16, 移転済み）など |

→ **大量の死活不明フィードを抱えた状態から始まる**ため、以下が設計上の必須要件になる。

1. 取り込み時に重複を排除し、OPML のフォルダを**カテゴリ**として保持する。
2. **死活チェック**コマンドで生存確認し、連続失敗したフィードは自動で無効化する（削除はしない）。
3. **リダイレクト（301）を検出して URL を自動更新**する（http→https、移転など）。
4. **初回実行で通知が殺到しないよう**、初回は既存記事を「既読化」するだけにする（`--backfill N` で直近 N 件のみ保存可能）。

### 2.1 開発用の小さな OPML（sample.opml）

取り込みのテスト用の小さな OPML（公開のフィード 11 件。入れ子のフォルダと重複を含む）。

## 3. 全体構成

```
                    ┌──────────── config.toml（設定） ────────────┐
                    │                                             │
 OPML ──import──▶ SQLite (feeds / items) ◀── 状態管理（既読・ETag・失敗回数）
                    │
  run ─────────────▼─────────────────────────────────────────────────────────
  [Source]            [Filter]                                 [Output]
  rss ──┐       ┌─ dedupe（既読判定）                    ┌─ markdown（ファイル保存）
  html ─┼─ Item ┼─ fulltext（trafilatura で本文抽出）──▶ ┼─ discord（Webhook 通知）
  cmd ──┘       ├─ filter（キーワード/カテゴリ除外）     └─ （将来）ntfy / メール 等
   (将来)       └─ summarize（LLM 要約・任意）
```

- **Item**（共通データ型）: `feed_id, guid, url, title, author, published, content_html, content_md, summary, category, tags`
- 各段はプラグイン（Python クラス）で、`config.toml` の並び順でパイプラインを組む。

## 4. 技術選定（軽さ重視）

| 用途 | 採用 | 理由 |
|---|---|---|
| 言語・管理 | Python 3.12 + uv | trafilatura（日本語本文抽出の精度が高い）を使うため |
| フィード解析 | feedparser | RSS 0.9x/1.0(RDF)/2.0/Atom すべて対応。古い RDF フィードも多いため |
| HTTP | httpx（asyncio） | 並列取得 + ホスト単位の同時接続制限 + 条件付き GET |
| 本文抽出 → MD | trafilatura | `output_format="markdown"` で直接 Markdown 化 |
| OPML 解析 | 標準 `xml.etree` | 追加依存なし |
| 設定 | TOML（標準 `tomllib`） | 追加依存なし |
| 状態管理 | SQLite（標準 `sqlite3`） | サーバー不要・1ファイル |
| LLM | llama.cpp `llama-server`（OpenAI 互換 API） | Ollama より軽量。API 互換なので Ollama にも差し替え可 |
| 要約モデル | **LFM2.5-1.2B-JP** Q4_K_M（約 700MB） | 軽さ優先。代替: Sarashina2.2-1B-Instruct |

依存パッケージは **feedparser / httpx / trafilatura** の 3 つ（＋それらの依存）に抑える。

### 実行環境（このマシン）
- 16 スレッド / メモリ 11GB（WSL2）→ 1.2B Q4 モデルは余裕で動作する見込み。
- 定期実行は cron か systemd user timer。WSL2 は Windows 側の再起動・シャットダウンで止まる点に注意（後述）。

## 5. データ設計（SQLite）

```sql
feeds(
  id INTEGER PRIMARY KEY,
  url TEXT UNIQUE,            -- xmlUrl（リダイレクト時は自動更新）
  original_url TEXT,          -- OPML 取り込み時の URL（記録用）
  title TEXT, site_url TEXT, category TEXT,
  source_type TEXT DEFAULT 'rss',   -- rss / html / cmd（将来）
  enabled INTEGER DEFAULT 1,
  disabled_reason TEXT,       -- 'gone_410', 'fail_x5', 'manual' など
  etag TEXT, last_modified TEXT,    -- 条件付き GET 用
  last_fetched_at TEXT, last_success_at TEXT,
  fail_count INTEGER DEFAULT 0,
  options TEXT                -- JSON: フィード個別設定（全文取得 off など）
)

items(
  id INTEGER PRIMARY KEY,
  feed_id INTEGER REFERENCES feeds(id),
  key TEXT,                   -- guid、なければ正規化した link のハッシュ
  url TEXT, title TEXT, published TEXT, fetched_at TEXT,
  status TEXT,                -- seen / saved / summarized / notified / error
  md_path TEXT, summary TEXT, error TEXT,
  UNIQUE(feed_id, key)
)
```

- 処理状態を `status` で持つため、途中で失敗しても**次回実行時に続きから再開**できる（例: 保存済みだが未通知のものを再通知）。

## 6. 主要な処理の詳細

### 6.1 OPML 取り込み / 書き出し
- `import-opml FILE [--category-prefix X]`: ネストしたフォルダ → `category`（多段なら `a/b`）。既存 URL はスキップ（タイトル・カテゴリのみ更新は `--update`）。
- `export-opml FILE`: 有効なフィードのみ / 全件を選択して書き出し（他のリーダーへの移行やバックアップ用）。

### 6.2 取得（rss ソース）
- 条件付き GET（ETag / Last-Modified）で、更新がないフィードは 304 で即終了 → 333 件でも軽い。
- 並列数は全体 16・同一ホスト 2 程度に制限（livedoor/ameba などに同一ホストが多いため）。
- 301 → URL 更新、404/410 → 即無効化候補、その他エラー → `fail_count++`、5 回連続で自動無効化。

### 6.3 全文取得（fulltext フィルタ）
- 記事 URL を取得して trafilatura で本文を Markdown 化する。
- 失敗した場合や本文が短すぎる場合は、フィードの `content` / `summary` を html→md 変換して使う。
- フィード単位で off にできる（全文配信済みのフィードや、まとめサイトなど）。

### 6.4 Markdown 保存（markdown アウトプット）
- パス: `archive/{category}/{YYYY}/{MM}/{YYYY-MM-DD}_{slug}.md`（slug はタイトルから。長さ制限・禁止文字除去）
- Front Matter（Obsidian 等でも扱いやすい形式）:
  ```markdown
  ---
  title: "記事タイトル"
  url: https://example.com/post/1
  feed: "フィード名"
  category: tech
  published: 2026-11-07T09:00:00+09:00
  fetched: 2026-11-07T10:15:00+09:00
  summary: |
    LLM 要約（ある場合）
  ---

  本文 Markdown …
  ```

### 6.5 要約（summarize フィルタ・任意）
- OpenAI 互換 API（`llama-server`）を呼ぶ。
- **入力は本文の先頭 約 3,000 字に切り詰める**（CPU ではプロンプトの読み込みが遅く、1B 級は長文で品質も落ちるため）。
- 出力形式は「3 行の箇条書き」に固定し、タイムアウト時は要約なしで先へ進む（要約は後から再実行できる）。
- カテゴリ単位で on/off を切り替えられる（全件要約すると重いため）。

### 6.6 Discord 通知（discord アウトプット）
- Webhook で Embed を送る（タイトル・URL・フィード名・要約・公開日時）。
- **カテゴリごとに Webhook を振り分けられる**（OPML のフォルダ = Discord チャンネル、という運用が可能）。
- レート制限: 429 のときは `retry_after` に従う。1 回の実行で多数ある場合は **1 メッセージに最大 10 Embed** まとめて送る。
- 通知モードは 2 種類: `item`（1 記事 = 1 Embed）と `digest`（実行ごとに件名一覧を 1 メッセージ）。

## 7. CLI 案（仮称: `feedpipe`）

```
feedpipe import-opml feedly.opml       # OPML 取り込み
feedpipe export-opml out.opml          # 書き出し
feedpipe check [--disable-dead]        # 全フィードの死活チェック
feedpipe feeds list [--disabled] [--category tech]
feedpipe feeds add URL --category tech
feedpipe feeds enable|disable ID
feedpipe run [--dry-run] [--category X] [--no-notify] [--no-summary]
feedpipe run --init                    # 初回: 既読化のみ（通知しない）
feedpipe retry                         # 途中で失敗した記事（未保存・未要約・未通知）を再処理
```

## 8. 設定ファイル案（config.toml）

```toml
[general]
db = "data/feedpipe.db"
archive_dir = "archive"
concurrency = 16
per_host = 2
user_agent = "feedpipe/0.1"

[pipeline]
filters = ["dedupe", "fulltext", "summarize"]
outputs = ["markdown", "discord"]

[fulltext]
min_length = 200          # これより短ければフィード本文を使う

[summarize]
enabled_categories = ["tech", "news"]
endpoint = "http://127.0.0.1:8080/v1"
model = "LFM2.5-1.2B-JP"
max_input_chars = 3000
timeout = 120

[discord]
mode = "item"             # item / digest
default_webhook = "env:DISCORD_WEBHOOK_DEFAULT"   # 秘密情報は環境変数から読む

[discord.webhooks]
tech = "env:DISCORD_WEBHOOK_TECH"
game = "env:DISCORD_WEBHOOK_GAME"
```

## 9. 実装フェーズ

| フェーズ | 内容 | 完了の目安 |
|---|---|---|
| P1 ✅ | プロジェクト雛形、SQLite、**OPML 取り込み/書き出し**、`check`（死活チェック） | feedly.opml を取り込み、生きているフィードが判明する |
| P2 | rss ソース、dedupe、fulltext、markdown 保存、`run --init` | 新着記事が全文 Markdown で保存される |
| P3 | Discord 通知（item/digest、カテゴリ別 Webhook、`retry`） | 新着が Discord に届く |
| P4 | llama-server + LFM2.5-1.2B-JP で要約、カテゴリ別 on/off | 要約付きで通知される |
| P5 | cron / systemd timer による定期実行、ログ | 放置で動き続ける |
| P6（将来） | html ソース（CSS セレクタ）、cmd ソース、キーワードフィルタ | RSS 以外も監視できる |

## 10. 未決事項（要確認）

1. **Discord の通知単位**: 1 記事ずつ（item）か、まとめ（digest）か。既定値をどちらにするか。
2. **カテゴリ別チャンネル**: OPML のフォルダごとに Webhook を分けるか、1 チャンネルに集約するか。
3. **保存先**: `archive/` をこのリポジトリ内に置くか、Obsidian Vault など外部のパスにするか。
4. **取得間隔**: 30 分 / 1 時間 / それ以上。
5. **WSL2 での定期実行**: WSL 内の cron/systemd でよいか（Windows を起動していないと止まる）。常時稼働させる別マシンの予定があるか。
6. **要約の対象**: 全件か、指定したカテゴリだけか。
7. **ツール名**: `feedpipe`（仮）でよいか。
