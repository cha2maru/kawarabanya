# feedpipe

RSS を全文 Markdown で保存し、Discord に通知するパイプライン（開発中）。設計は [DESIGN.md](DESIGN.md) を参照。

## 使い方

### 記事の取り込み

```sh
uv run feedpipe run                          # 新着を取得し、全文 Markdown を archive/ に保存
uv run feedpipe run --backfill 3             # 初回のフィードは、既読化に加えて新しい順に 3 件保存
uv run feedpipe run --category tech --no-fulltext
uv run feedpipe feeds option ID no_fulltext true   # このフィードは元記事を取りに行かない
```

- **初回のフィードは既存記事を既読にするだけ**で保存しない（通知の殺到を防ぐため）。2 回目以降に増えた記事が保存される。
- 保存先: `archive/{カテゴリ}/{年}/{月}/{日付}_{タイトル}.md`（Front Matter 付き）
- 本文は元記事から trafilatura で抽出し、取れない・短すぎる場合はフィード内の本文を使う（Front Matter の `source` に記録）。

### フィード管理

```sh
uv run feedpipe [-v] import-opml sample.opml # OPML を取り込む（-v で除外した重複を表示）
uv run feedpipe check                        # 死活チェック（301/308 で移転していれば URL を更新）
uv run feedpipe check --disable-dead         # 404/410・ホスト消滅・非フィードは即無効化
uv run feedpipe feeds list [--all|--disabled] [--category tech]
uv run feedpipe feeds add URL --category tech
uv run feedpipe feeds enable|disable ID...
uv run feedpipe export-opml out.opml [--all]
```

- DB は既定で `data/feedpipe.db`（`--db` か `config.toml` の `[general] db` で変更可）。
- 連続 5 回失敗したフィードは自動で無効化される（`max_failures`）。

## テスト

```sh
uv run pytest
```

### Discord（案A: カテゴリごとのフォーラムに 1 記事 = 1 投稿）

事前に Bot を作り、`.env` に `DISCORD_BOT_TOKEN=...` を書く。フォーラムを使うにはサーバーの「コミュニティ」を有効にしておく必要がある。

```sh
uv run feedpipe discord invite-url           # Bot の招待 URL
uv run feedpipe discord setup [--apply]      # カテゴリごとにフォーラム（タグ = フィード名）と Webhook を作る
uv run feedpipe notify [--dry-run]           # 保存済み・未通知の記事を投稿（run の最後にも自動で実行）
uv run feedpipe run --no-notify              # 通知しない（次回の run / notify で送られる）
```

### 要約（ローカル LLM）

`scripts/llama-server.sh` で llama.cpp の `llama-server`（既定モデル: Qwen3.5-4B Q4_K_M）を起動しておくと、
`run` が保存した記事を要約し、Front Matter の `summary` と Discord の投稿本文に使う。サーバーが止まっていれば要約なしで続行する。

```sh
scripts/llama-server.sh &                    # http://127.0.0.1:8080/v1
uv run feedpipe summarize                    # 保存済み・未通知で要約のない最近の記事を要約
uv run feedpipe run --no-summary             # 要約しない
```

- 要約と投稿は **公開から 3 日以内の記事だけ**（`[summarize] max_age_days` / `[discord] max_age_days`）。
  それより古い未通知の記事は投稿せず `stale` にする（Markdown の保存はする）。

### ジャンル（genres.toml）

投稿先のジャンルは `genres.toml`（git 管理外。書き方は `genres.example.toml`）で定義する。

- `[news]` がジャンル（Discord のフォーラム 1 つ）、`[news.world]` がサブジャンル（フォーラム内のタグ）
- フィードの移動は、URL の行を別のジャンルの `feeds` に移して反映する

```sh
uv run feedpipe genres list                  # ジャンルとフィード数
uv run feedpipe genres apply [--dry-run]     # genres.toml の割り当てをフィードに反映
uv run feedpipe discord setup --apply        # フォーラム・タグを更新（ジャンルに対応しないフォーラムは「未使用」と表示）
```

genres.toml に書かれていないフィードの記事は投稿されない（Markdown の保存はする）。
