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
