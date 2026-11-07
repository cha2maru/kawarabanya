# kawarabanya

個人用 Discord サーバーを管理する Bot（今ある機能は RSS）。構成と方針は DESIGN.md、使い方は README.md。

## ルール

- **DESIGN.md と README.md を、変更と同じコミットで更新する。**
  - README.md: コマンド・オプション・設定キー・セットアップ手順を追加・変更・削除したとき。
  - DESIGN.md: 構成・データ（テーブルや列、items.status）・処理の流れ・技術選定・方針が変わったとき、実測値を取り直したとき、未決事項が決まったとき（§7 から消して本文に反映する）。
  - 内部だけのリファクタリングやバグ修正で、上のどれにも当たらなければ更新しない。
- 購読データ（`feedly.opml`・`genres.toml`）と秘密情報（`.env`）はコミットしない。
- 変更したら `uv run pytest` と `uvx ruff check src tests` を通す。
