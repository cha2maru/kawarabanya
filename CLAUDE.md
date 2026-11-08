# kawarabanya

個人用 Discord サーバーを管理する Bot（今ある機能は RSS）。構成と方針は DESIGN.md、使い方は README.md。

## ルール

- **実装したら、同じコミットで次を確かめて更新する（実装の一部として毎回やる）。**
  1. `uv run pytest` と `uvx ruff check src tests` を通す（ruff の規則は pyproject.toml）。
  2. README.md: コマンド・オプション・設定キー・セットアップ手順を追加・変更・削除したとき。
  3. `.claude/skills/` のスキル（kawarabanya-install・kawarabanya-discord-setup・kawarabanya-ops）: コマンド・手順・決まり・doctor の項目を変えたとき。依頼とコマンドの対応表に足し忘れがないかも見る（スキルには手順と決まりだけを書き、詳しい説明は README.md に任せる）。
  4. 見本（`config.example.toml`・`genres.example.toml`・`.env.example`）: 設定キー・書き方を足したり変えたりしたとき。見本は git 管理する（`config.example.toml` が既定値と一致することは tests/test_config.py で確かめる）。
  5. DESIGN.md: 構成・データ（テーブルや列、items.status）・処理の流れ・技術選定・方針が変わったとき、実測値を取り直したとき、未決事項が決まったとき（§7 から消して本文に反映する）。
  6. CHANGELOG.md: 利用者に見える変更（機能・コマンド・設定・直した不具合）は、次の版の節（なければ「## 未リリース」を作る）に 1 行足す。
  - 内部だけのリファクタリングで、2〜6 のどれにも当たらなければ更新しない。
- 購読データ（`feedly.opml`・`genres.toml`）と秘密情報（`.env`）はコミットしない。
- コードは [docs/CODING.md](docs/CODING.md) の規約に沿って書く（構成・依存の向き・import・docstring など）。

## 定期の見直し

- **好みの語（週 1 回）**: 会話の最初の依頼に取りかかる前に `uv run kawarabanya prefs review --due` を実行する。何も出なければ（前回から 7 日たっていない）触れない。語が出たら:
  1. 名詞でない・記事の内容を表さない語（「二回」「目次」「運動部配信」のような語、どの話題にも出る一般的な語）を選び、理由を添えて利用者に見せる。
  2. 利用者が良いと言った語だけ `prefs ignore 語...` で外し、`prefs review --done` で残りも確認済みにする。
  3. 同じ形の語（数字＋助数詞など）が繰り返し出るなら、語を足すより `rss/prefs.py` の取り出し方を直す。
