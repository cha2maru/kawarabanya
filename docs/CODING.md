# コーディング規約

kawarabanya の Python の書き方。元にしたもの: [PEP 8](https://peps.python.org/pep-0008/)（書き方）、[PEP 257](https://peps.python.org/pep-0257/)（docstring）、[PEP 484](https://peps.python.org/pep-0484/)・[585](https://peps.python.org/pep-0585/)・[604](https://peps.python.org/pep-0604/)（型ヒント）、[PyPA の src レイアウト](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)。機械で確かめられるものは `pyproject.toml` の `[tool.ruff]` に書いてあり、`uvx ruff check src tests` で確かめる。

## 1. 構成と依存の向き

```
src/kawarabanya/
  core/   設定・SQLite・ローカル LLM・チャット（chat.py の Chat と、Discord・Slack の実装）   … 共通の土台
  rss/    RSS 機能（取り込み〜保存・判定・要約・投稿・まとめ）
    sources/  記事の取り込み元（RSS・HTML・拡張・サイトマップ・OPML・RSSHub の確認）
  bot/    常駐 Bot と、Discord から日本語で頼まれたことの解釈
  cli/    コマンド（引数の定義と各コマンド）
```

- **依存は一方通行**: `cli`・`bot` → 機能のパッケージ（`rss`・`schedule`）→ `core`。機能のパッケージどうしは import しない。`core` は上の層を import しない。`rss/sources` は `rss` の上の部分（pipeline・notify など）を import しない。
- 新しい機能（RSS 以外）は、`rss` と並べて機能ごとのパッケージにする。共通にしたくなったものだけ `core` に下ろす。
- 1 つのモジュールは 1 つの役割。300〜400 行を超えて役割が 2 つ見えてきたら分ける。

## 2. 名前（PEP 8）

- モジュール・関数・変数: `snake_case`。クラス: `CapWords`。定数: `UPPER_CASE`（モジュールの先頭にまとめる）。
- モジュールの中だけで使うものは先頭に `_`。
- 略語は避け、分かる名前にする（`cfg`・`conn`・`db` などの慣用は使ってよい）。

## 3. import

- **絶対 import にそろえる**（`from kawarabanya.core import db`）。相対 import は使わない（ruff の TID252）。モジュールを動かしたときに直す場所が分かりやすい。
- 各ファイルの先頭に `from __future__ import annotations`。
- 並びは 標準ライブラリ → 外部 → kawarabanya の順（ruff の I が並べる）。
- 重い・特定のコマンドでしか使わないもの（discord.py など）は、使う関数の中で import する。

## 4. 型ヒント（PEP 484・585・604）

- 公開する関数・メソッドは、引数と戻り値に型を書く。
- `list[str]`・`dict[str, int]`・`X | None` の書き方を使う（`List`・`Optional` は使わない）。
- 値のまとまりは `@dataclass`（DB の行は `sqlite3.Row` のまま使ってよい）。

## 5. docstring とコメント（PEP 257）

- 公開するモジュール・クラス・関数には、日本語の docstring を書く。1 行目は要約で、末尾は「。」。詳しい説明は空行のあとに書く。
- 引数の説明の節（`Args:` など）は作らず、文の中で説明する（引数の名前と型で分かることは書かない）。
- モジュールの docstring には、そのモジュールの役割と、設定の書き方など使う人が知りたいことを書く。
- コメントは「なぜそうするか」を日本語で書く（何をしているかはコードで分かるようにする）。実測や試した結果に基づくなら、その数字も書く。
- 分かっている制限のある近道は `# shortcut: <制限>、<直すきっかけ>` と書く。

## 6. エラーの扱い

- 利用者が直せるもの（設定の誤り・見つからない）は `ValueError` に日本語のメッセージを付けて投げる。CLI が「エラー: …」として表示する。
- 外部とのやりとり（HTTP・LLM・拡張）の失敗で全体を止めない。1 記事・1 フィードの失敗は記録して続ける。何でも捕まえる `except Exception` は境界だけで使い、`# noqa: BLE001 <理由>` を付ける。
- データを失う・壊すおそれのある処理（削除・上書き）は、確認してから行う作りにする（CLI は `--apply`、Bot はボタン）。

## 7. 文字列・ファイル・非同期

- ファイルは `pathlib.Path` で扱い、読み書きは `encoding="utf-8"` を書く。保存するものは常に UTF-8。
- HTTP は `httpx`（取り込みは非同期の `AsyncClient`）。重い同期処理（本文の抽出など）は `asyncio.to_thread` で逃がす。
- 何十分もかかる処理（取り込み・まとめ）は、Bot から別プロセスで実行する（イベントループを止めない）。

## 8. テスト

- `pytest`。`tests/test_<対象>.py` に書く。実行は `uv run pytest`。
- ネットワークにつながない（`httpx.MockTransport`）。ファイルは `tmp_path`、DB は `:memory:`。
- 分岐・ループ・パーサーなど、新しい処理には小さなテストを付ける。

## 9. 書式

- 1 行は 120 字まで（日本語のコメント・文字列で超えるのは許す）。
- インデントは空白 4 つ。文字列は `"`。
