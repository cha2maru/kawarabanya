---
name: kawarabanya-ops
description: kawarabanya を CLI で操作する（サイトの追加・削除、OPML の取り込み・書き出し、ジャンルの好き嫌い、取り込み・まとめの実行、状態の確認、ログ、更新・再起動）。「サイトを追加して」「スポーツはまとめから外して」「まとめを出して」「いまどうなってる」「Bot を再起動して」など、kawarabanya の日々の操作を頼まれたときに使う。
---

# kawarabanya の操作

リポジトリのルートで `uv run kawarabanya <コマンド>` を使う。コマンドとオプションの正本は README.md の「コマンド」の表と `uv run kawarabanya <コマンド> --help`。

## 依頼とコマンド

| 依頼 | コマンド |
|---|---|
| いまどうなってる / 動いてる？ | `status`、不調なら `doctor` |
| サイトを追加 | `feeds add URL --genre <ジャンル>`（ジャンルが分からなければ `--genre auto`。RSS が見つからなければ表示される案内（RSSHub・sitemap・HTML 取り込み）に従う） |
| サイトを外す | `feeds remove <ID か URL>`（ID は `feeds list` で調べる。記事と投稿は残る） |
| OPML を取り込む / 書き出す | `import-opml FILE --genre auto` / `export-opml FILE` |
| RSSHub で取れる？ | `feeds rsshub URL` |
| ○○は好き / まとめに出さない / 要らない | `genres interest <ジャンルかサブジャンル> like / low / mute`（戻すときは `normal`） |
| ジャンルの一覧 | `genres list` |
| 今すぐ取り込む | `run`（数分〜数十分。バックグラウンドで実行して終わりを待つ） |
| 今すぐまとめを出す / 直す | `digest`（前回のまとめ以降）/ `digest --update` |
| 新しいサブジャンルを作る | `genres suggest`（案の表示）→ よければ `genres suggest --apply` |
| genres.toml を手で直した | `genres apply --dry-run` で確かめてから `genres apply`、タグも変えたら `discord setup --apply` |
| ログ | `journalctl --user -u kawarabanya-bot -n 100 --no-pager` |
| 更新 | `git pull && uv sync && systemctl --user restart kawarabanya-bot`（DB のマイグレーションは起動時に自動。前にバックアップを取る） |

## 決まり

- **先に見るだけの実行をする**: `--dry-run` や `--apply` なしのあるコマンドは、まずそれで結果を見せてから本番を実行する（`genres apply`・`genres suggest`・`discord setup`・`discord delete-duplicates`・`notify`・`digest`）。
- **取り消せない操作は利用者に実行してもらう**: `discord setup --apply --prune`・`discord delete-duplicates --apply`・`digest --delete`。何が消えるかを見せ、`! uv run kawarabanya ...` の形でコマンドを示す。
- **Discord への投稿**（`run`・`notify`・`digest`）は利用者の Discord に出る。頼まれたときだけ実行する。
- **genres.toml は Bot も書き換える**（サブジャンルの自動作成・Discord からの依頼）。手で直す前に今の中身を読み直し（git 管理外なので差分では分からない）、直したら `genres list` で読めることを確かめる（壊れると Bot の定期処理が止まる）。
- 長い処理（`run`・`import-opml --genre auto`・`genres suggest`）を Bot の定期処理（毎時・6:30・18:30）と同時に動かすと、要約サーバーを取り合って遅くなる。急がなければ Discord から頼む（Bot が順番に実行する）。
