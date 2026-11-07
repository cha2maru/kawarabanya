---
name: kawarabanya-discord-setup
description: kawarabanya の Discord 側を準備する（Bot のアプリ作成・トークン・MESSAGE CONTENT INTENT・招待・コミュニティ・フォーラムとタグ・まとめの投稿先）。「Discord の準備をして」「Bot を招待したい」「フォーラムを作って」「Discord から頼んでも反応しない」と頼まれたときに使う。
---

# kawarabanya の Discord の準備

Developer Portal とサーバーの設定は、利用者にしかできない（ブラウザで本人がログインして操作する）。手順を短く示し、終わったら知らせてもらって、こちらは CLI で確かめる。

## 手順

1. **アプリと Bot**（利用者）: https://discord.com/developers/applications → New Application → 左の「Bot」→ Reset Token。
2. **トークン**（利用者）: `.env` の `DISCORD_BOT_TOKEN=` に貼ってもらう。トークンをチャットに貼らせない・こちらで表示しない。貼られたら `uv run kawarabanya doctor` で「Discord の Bot」が ✅ か確かめる。
3. **MESSAGE CONTENT INTENT**（利用者）: 同じ「Bot」の画面の「Privileged Gateway Intents」（日本語の表示では「特権ゲートウェイインテント」）の MESSAGE CONTENT INTENT をオン → Save Changes。下の「Bot の権限」の計算ツールは触らなくてよい。`doctor` の該当行が ✅ になる。
4. **招待**: `uv run kawarabanya discord invite-url` の URL を開いてもらい、サーバーを選ぶ。サーバーの設定で「コミュニティ」を有効にしてもらう（フォーラムに必要）。まとめの投稿先のテキストチャンネル（既定 `#ニュース`、`config.toml` の `[digest] channel`）を作ってもらう。
5. **フォーラムとタグ**: `uv run kawarabanya discord setup`（予定の表示だけ）を見せ、問題なければ `uv run kawarabanya discord setup --apply`。ジャンル（genres.toml のトップレベル）ごとにフォーラム、サブジャンルごとにタグ、Webhook ができる。
6. **確かめる**: `uv run kawarabanya doctor` が ✅、`uv run kawarabanya run` で投稿されるか、Bot が動いていれば Discord で「@Bot いまどうなってる？」に返事が来るか。

## うまくいかないとき

| 症状 | 原因と直し方 |
|---|---|
| メンションしても「本文が読めませんでした」 | MESSAGE CONTENT INTENT がオフ（3）。オンにしたら Bot を再起動する（`systemctl --user restart kawarabanya-bot`） |
| メンションしても何も起きない | Bot が止まっている（`journalctl --user -u kawarabanya-bot -n 50`）か、持ち主以外が頼んでいる（頼めるのはサーバーの持ち主だけ） |
| `discord setup` で 403 / Missing Access | Bot のロールに権限が足りない。`discord invite-url` の URL で招待し直すか、サーバー設定 → ロール → Bot のロールで「チャンネルの管理・ウェブフックの管理・スレッドの管理・メッセージを送る」をオン |
| タグの付け直し（`discord retag`）が 403 | Bot のロールに「スレッドの管理」 |
| フォーラムが作れない | サーバーの「コミュニティ」が無効 |

## 決まり

- `discord setup --apply --prune`（ジャンルにないフォーラムを中の投稿ごと削除）・`discord delete-duplicates --apply`・`digest --delete` は取り消せない。こちらで実行せず、何が消えるかを `--apply` なし（または予定の表示）で見せてから、利用者に実行してもらう。
- Webhook の名前（`[discord] webhook_name`）は運用中に変えない（投稿済みのまとめを書き換え・削除できなくなる）。
