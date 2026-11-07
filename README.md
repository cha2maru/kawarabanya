# feedpipe

RSS を全文 Markdown で保存し、Discord に通知するパイプライン（開発中）。設計は [DESIGN.md](DESIGN.md) を参照。

## 使い方（P1: フィード管理）

```sh
uv run feedpipe import-opml sample.opml      # OPML を取り込む（-v で除外した重複を表示）
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
