---
name: kawarabanya-slack-setup
description: kawarabanya の Slack 側を準備する・直す（Slack アプリの作成・Bot のトークン・権限・Socket Mode・イベント・チャンネルの作成・Discord と両方に投稿する設定）。「Slack にも流したい」「Slack に移りたい」「Slack の準備をして」「Slack で @kawarabanya に反応しない」「not_in_channel / missing_scope が出る」と頼まれたら使う。Slack 一般の質問や、kawarabanya 以外のアプリには使わない。Discord 側は kawarabanya-discord-setup。
---

# kawarabanya の Slack の準備

Slack のアプリの設定（https://api.slack.com/apps）は利用者にしかできない（ブラウザで本人がログインして操作する）。手順を短く示し、終わったら知らせてもらって、こちらは CLI で確かめる。詳しい説明は README の「Slack」。

## 手順

1. **アプリ**（利用者）: https://api.slack.com/apps →「Create New App」→「From a manifest」→ ワークスペースを選び、リポジトリの `slack-manifest.json` の中身を貼る（権限・Socket Mode・イベント・ボタンの設定が入っている）。
2. **Bot のトークン**（利用者）: 「Install to Workspace」→ Bot User OAuth Token（`xoxb-`）を `.env` の `SLACK_BOT_TOKEN=` に貼ってもらう（行がなければ値を空にして足しておく）。トークンはチャットに貼らせない・こちらで表示しない（貼られたら「OAuth & Permissions」で Revoke してもらい、作り直す）。
3. **アプリのトークン**（利用者。Slack から @ で頼まないなら飛ばす）: 「Basic Information」→「App-Level Tokens」で `connections:write` のトークン（`xapp-`）を作り、`.env` の `SLACK_APP_TOKEN=` に。manifest では作れない。
4. **設定**: 利用者のメンバー ID（プロフィール →「︙」→「メンバー ID をコピー」、U で始まる。秘密ではない）を聞いて `config.toml` の `[slack] mention_user_id` に書く。頼めるのはこの人だけ。`[chat] targets` は `["slack"]`（Slack だけ）か `["discord", "slack"]`（両方）を利用者に選んでもらう。
5. **確かめる**: `uv run kawarabanya doctor` の「Slack の Bot」が ✅、権限が足りているか。
6. **チャンネル**: `uv run kawarabanya slack setup`（予定の表示だけ）を見せ、問題なければ `uv run kawarabanya slack setup --apply`。ジャンルごと（`kawarabanya-tech` など）・まとめ（`kawarabanya-digest`）・予定（`kawarabanya-schedule`、非公開）ができ、どれにも `mention_user_id` の人を招待する。DB の版が上がる（前に `bak-v<旧版>` を取る）ので、初めてのときはそのことを伝えてから。
7. **試す**: `uv run kawarabanya chat test tech "テスト"`（targets のすべてに送る）→ Bot を再起動（`systemctl --user restart kawarabanya-bot`。ログに「Slack に Socket Mode でつなぎました」）→ Slack で「@kawarabanya いまどうなってる？」に返事が来るか。

## うまくいかないとき

| 症状 | 原因と直し方 |
|---|---|
| `missing_scope` | 権限が足りない。`slack-manifest.json` を「App Manifest」に貼り直し、「Install to Workspace」をやり直す（トークンは変わらない） |
| `not_in_channel` | Bot がチャンネルにいない。公開チャンネルは `slack setup --apply` が入る。非公開は Slack でチャンネルに Bot を招待する |
| 投稿が見えない | 利用者がチャンネルに入っていない。`slack setup --apply`（`mention_user_id` を招待する）か、Slack でチャンネルに参加する |
| まとめのリンクが元記事に飛ぶ | その記事は Slack に投稿されていない（Slack に切り替える前の記事など）。Slack に投稿された記事は Slack の投稿に飛ぶ |
| 投稿者名がフィード名にならない | `chat:write.customize` がない（manifest を貼り直す） |
| @kawarabanya に反応しない | `SLACK_APP_TOKEN` がない（3）・Socket Mode かイベントがオフ（manifest を貼り直す）・Bot を再起動していない・`mention_user_id` 以外の人が頼んでいる（`journalctl --user -u kawarabanya-bot -n 50`） |
| ボタンを押しても何も起きない | Interactivity がオフ（manifest を貼り直す）か、10 分を過ぎた |

## 決まり

- 実行中の DB は版 5 以降（`channels`・`posts`）。古い版（Discord だけの kawarabanya）に戻すと開けないので、戻すときは `data/kawarabanya.db.bak-v4` を使う。
- `chat delete-duplicates --apply`・`digest --delete` は Slack の投稿も消す（targets のすべて）。kawarabanya-discord-setup の決まりと同じく、利用者に実行してもらう。
- 権限を足したら `core/slack_api.py` の `SCOPES` と `slack-manifest.json` を両方直す（テストが一致を確かめる）。
