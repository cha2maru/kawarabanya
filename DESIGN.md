# feedpipe 設計（v0.2）

最終更新: 2026-11-07

## 1. 目的と方針

- **個人用 Discord サーバーを管理する総合 Bot**。今ある機能は RSS（全文 Markdown 保存 → 要約 → Discord フォーラムに投稿）で、機能は今後足していく。
- 個人運用（利用者 1 人・サーバー 1 つ）なので、複数ユーザー・複数サーバーへの対応はしない。
- **軽さ優先**: SQLite + TOML + 1 プロセスの Bot。DB サーバー・Docker は使わない。LLM は CPU で動くローカルモデル。
- 骨組み（プラグイン機構など）は、使う機能が決まってから作る。

## 2. 全体構成

```
systemd --user: feedpipe-bot.service
└─ feedpipe bot（常駐。discord.py で Gateway に接続）
   ├─ 子プロセス: scripts/llama-server.sh（llama.cpp + Qwen3.5-4B、OpenAI 互換 API :8080）
   └─ 60 分ごとに子プロセス: feedpipe run
        取得 ─▶ 既読判定 ─▶ 全文抽出 ─▶ Markdown 保存 ─▶ 要約 ─▶ 投稿 ─▶ 👍 集計
        (httpx)  (SQLite)   (trafilatura) (archive/)     (LLM)   (Webhook) (Bot API)

設定・データ:
  config.toml   … 動作設定（git 管理外）        .env         … Bot トークン（git 管理外）
  genres.toml   … ジャンルとフィードの割り当て（git 管理外。見本は genres.example.toml）
  data/feedpipe.db … SQLite（feeds / items / discord_channels）
  archive/      … 記事の Markdown
```

- `run` を別プロセスにするのは、要約に数十分かかっても Bot のイベントループ（Discord との接続維持）を止めないため。前回の `run` が終わるまで次は始めない。
- 投稿は Webhook、チャンネル・タグの作成と 👍 の読み取りは Bot トークン（REST）。どちらも同じ Bot（同じ招待 URL）で管理する。

## 3. 主な処理

### 3.1 フィード管理
- `import-opml`: ネストしたフォルダは `a/b` のカテゴリになる。重複 URL は最初の 1 件だけ。
- `check`: 301/308 の移転で、移転先が正常なときだけ URL を更新（`original_url` は残す）。404/410・ホスト消滅・非フィードは `--disable-dead` で無効化。それ以外の失敗は 5 回連続で自動無効化。最新記事の日時を記録し、1 年以上更新のないものを報告する。
- `genres.toml` が投稿先の正本。書かれていない有効なフィードは分類なし（投稿されない）になり、`--disable-unassigned` で取得も止める。

### 3.2 取得と保存（run）
- 条件付き GET（ETag / Last-Modified）。全体 16・同一ホスト 2 の同時接続制限。
- **初回のフィードは既存記事を既読にするだけ**（`--backfill N` で新しい順に N 件だけ保存）。フィード内の重複記事は先に除く。
- 本文は元記事から trafilatura で Markdown 抽出。取れない・短すぎる（200 字未満）ときはフィードの本文と長い方を使い、Front Matter の `source` に記録する。
- 保存先: `archive/{カテゴリ}/{年}/{月}/{日付}_{タイトル}.md`（Front Matter: title / url / feed / category / published / fetched / source / summary）。

### 3.3 要約
- llama-server（OpenAI 互換 API）に本文の先頭 3,000 字を渡し、2〜3 文・120 字程度で要約させる。思考モードは切る。日本語と英数字の間の余分な空白は除く。
- **公開から 3 日以内の記事だけ**要約する（投稿しない古い記事に CPU を使わない）。
- サーバーに繋がらなければ、その回は要約なしで続ける（後から `summarize` で補える）。

### 3.4 Discord
- **ジャンル 1 つ = フォーラム 1 つ**（親カテゴリ「feedpipe」の下。年齢制限オン）。**サブジャンルとフィード名はフォーラムのタグ**（上限 20 個、名前は 20 字）。
- **1 記事 = 1 投稿**: スレッド名 = 記事タイトル、表示名 = フィード名、本文 = 要約（なければ本文の先頭 200 字）＋公開日時＋URL。メンションは無効化。
- 公開から 3 日を過ぎた未通知の記事は投稿せず `stale` にする。1 回の実行で投稿するのは 30 件まで。
- `discord setup` はジャンル構成に合わせてフォーラム・タグ・Webhook・既定リアクションを揃える（何度実行しても同じ結果）。`--prune` で対応しないフォーラムを削除する。
- **👍 は「興味あり」の印**として集計するだけで、投稿の取捨選択には使わない（押されていない投稿は「見ていない」可能性があるので、興味なしとは扱わない）。投稿から 7 日以内の記事を 6 時間ごとに確認する。

## 4. データ（SQLite）

| テーブル | 主な列 |
|---|---|
| `feeds` | url（移転で更新）/ original_url / title / category（= ジャンルのパス）/ enabled / disabled_reason / etag / last_modified / last_status / fail_count / latest_entry_at / initialized_at / options（JSON） |
| `items` | feed_id / key（guid か link）/ url / title / published / status / md_path / summary / notified_at / discord_thread_id / likes / likes_checked_at |
| `discord_channels` | category（トップレベルのジャンル）/ channel_id / webhook_url / tags（JSON: `feed:<id>` と `sub:<パス>` → タグ ID） |

`items.status`: `seen`（初回の既読化）→ `new` → `saved` → `notified`。投稿しなかったものは `stale`、失敗は `error`。状態を記録するので、途中で落ちても次の実行で続きから処理する。列の追加は `db.ADDED_COLUMNS` で自動的に行う。

## 5. 技術選定

| 用途 | 採用 | 理由 |
|---|---|---|
| 言語・管理 | Python 3.12 + uv | trafilatura を使うため |
| フィード解析 | feedparser | RDF を含む古い形式に対応 |
| HTTP | httpx（asyncio） | 並列取得・同時接続制限・条件付き GET |
| 本文抽出 | trafilatura | 日本語の精度が高く、Markdown を直接出せる |
| Discord | discord.py（常駐）＋ httpx（REST・Webhook） | |
| LLM | llama.cpp `llama-server` + **Qwen3.5-4B** Q4_K_M | 下の比較による。OpenAI 互換なので他の API にも替えられる |
| 設定・DB | TOML（tomllib）・SQLite（sqlite3） | 標準ライブラリ |

### 要約モデルの比較（tech 8 記事・CPU 8 スレッド）

| | LFM2.5-1.2B-JP | Qwen3-4B-Instruct-2507 | Qwen3.5-4B |
|---|---|---|---|
| メモリ | 約 1.4GB | 約 6.1GB | 約 6.0GB |
| 正確さ | ✕ 本文にない日付を作った | ○ | ○ |
| 全体の傾向 | ✕ 1 件目の言い換え | ○ | ◎ |

LFM（軽さ優先で最初に選んだ）は事実の誤りがあったため、Qwen3.5-4B に変更した。

## 6. 実測値（2026-11-07）

| 項目 | 値 |
|---|---|
| feedly.opml | 333 フィード → つながる 189 → 1 年以内に更新あり 103 → 重複を除いて **94 件をジャンルに割り当て** |
| 記事の量 | tech だけで 1 日約 120 件。news は重複フィード 9 件を外して約 160 件/日（外す前は 500 件超） |
| Qwen3.5-4B の速さ（CPU） | 読み込み約 130 トークン/秒、出力約 14 トークン/秒 → **要約 1 件 約 16 秒** |
| 全件要約した場合 | 1 日 400 件で約 1.8 時間の CPU |

## 7. 今後の候補・未決事項

1. **1 日のまとめ（ジャンルごとのダイジェスト）**: 見出しの一覧から「今日の傾向」と「主な話題 5 つ」を作る試作中。1 日 100 件を超えると全件の一覧は載せられない（Discord は 1 メッセージ 2,000 字、LLM の入力は 8,192 トークン）。
2. **要約前のふるい（Jev 風の判定）**: 見出し＋冒頭で「要約・投稿する価値があるか」を 1 トークン＋確率で判定する（見込み 1.2 秒/件）。CPU 時間を 1 日 107 分 → 40 分程度に減らせる見込み。精度は 👍 のデータと照らして確かめる。
3. **スラッシュコマンド**: `/feed add` など。次に作る機能が決まってから。
4. **WSL2 での常駐**: Windows が起動している間だけ動く。WSL の起動時に自動で立ち上げるには `loginctl enable-linger` が必要。
5. **プロジェクト名**: Bot 全体の名前にするか（今は `feedpipe`）。
6. タグ名が 20 字で途中から切れる（必要なら genres.toml でフィードごとの短い名前を指定できるようにする）。
