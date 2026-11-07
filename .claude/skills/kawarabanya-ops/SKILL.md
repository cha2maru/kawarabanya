---
name: kawarabanya-ops
description: kawarabanya を CLI で操作する（サイトの追加・削除・一時停止、OPML の取り込み・書き出し、ジャンルの好き嫌い、取り込み・まとめの実行と書き換え、状態の確認、👍 の傾向、タグの付け直し、重複投稿の整理、ログ、更新・再起動）。「このサイト登録して」「GIGAZINE もう要らない」「スポーツはまとめから外して」「まとめ出して」「いまどうなってる」「最近エラー出てない？」「Bot 再起動して」「アップデートして」など、kawarabanya の日々の運用を頼まれたら、コマンド名を言われなくても使う。kawarabanya のコード自体を直す・機能を足す依頼では使わない（それは通常の開発）。
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
| ○○は好き / 優先度を下げる / まとめに出さない / 要らない | ジャンルなら `genres interest <ジャンルかサブジャンル> like / down / low / mute`（戻すときは `normal`）。サイトなら `feeds option <ID> interest down`（戻すときは値を省く。サイトの設定がジャンルより優先） |
| ジャンルの一覧 | `genres list` |
| 今すぐ取り込む | `run`（数分〜数十分。バックグラウンドで実行して終わりを待つ） |
| 今すぐまとめを出す / 直す | `digest`（前回のまとめ以降）/ `digest --update` |
| 新しいサブジャンルを作る | `genres suggest`（案の表示）→ よければ `genres suggest --apply` |
| genres.toml を手で直した | `genres apply --dry-run` で確かめてから `genres apply`、タグも変えたら `discord setup --apply` |
| つながらないサイトを止める | `check`（結果の表示）→ `check --disable-dead`（404 や DNS の失敗など、戻りそうにないものを無効にする） |
| サイトを一時的に止める / 戻す | `feeds disable <ID>` / `feeds enable <ID>` |
| 好みの傾向（👍） | `interests --days 30`（`likes` で 👍 を取り直す） |
| 投稿のタグを付け直す | `discord retag`（Bot のロールに「スレッドの管理」が要る） |
| 重複した投稿を消す | `discord delete-duplicates`（一覧）→ 消すのは利用者（下の「決まり」） |
| まとめを消す | `digest --delete [--genre G] [--day YYYY-MM-DD]` → 利用者に実行してもらう |
| Discord に不調の通知が来た | フィードなら `feeds list --disabled` で原因を見て、直ったら `feeds enable <ID>`。RSSHub なら `docker start rsshub`。定期処理ならログを見る |
| ログ | `journalctl --user -u kawarabanya-bot -n 100 --no-pager` |
| 更新 | `git pull && uv sync && systemctl --user restart kawarabanya-bot`（DB のマイグレーションは起動時に自動。前にバックアップを取る） |

## 決まり

- **先に見るだけの実行をする**（フィードの移動・タグの作り直し・投稿は、間違えると直すのに手間がかかるので）: `--dry-run` や `--apply` なしのあるコマンドは、まずそれで結果を見せてから本番を実行する（`genres apply`・`genres suggest`・`discord setup`・`discord delete-duplicates`・`notify`・`digest`）。
- **取り消せない操作は利用者に実行してもらう**（Discord の投稿やフォーラムは消すと戻せず、どれを消すかは利用者が決めることなので）: `discord setup --apply --prune`・`discord delete-duplicates --apply`・`digest --delete`。何が消えるかを見せ、`! uv run kawarabanya ...` の形でコマンドを示す。
- **Discord への投稿**（`run`・`notify`・`digest`）は利用者のサーバーに出て、ほかの人にも見える。試しに動かすときは `--dry-run`（`notify`・`digest`）か `run --no-notify` を使い、本当に投稿するのは頼まれたときだけ。
- **genres.toml は Bot も書き換える**（サブジャンルの自動作成・Discord からの依頼）。手で直す前に今の中身を読み直し（git 管理外なので差分では分からない）、直したら `genres list` で読めることを確かめる（壊れると Bot の定期処理が止まる）。
- 長い処理（`run`・`import-opml --genre auto`・`genres suggest`）を Bot の定期処理（毎時・6:30・18:30）と同時に動かすと、要約サーバーを取り合って遅くなる。急がなければ Discord から頼む（Bot が順番に実行する）。
