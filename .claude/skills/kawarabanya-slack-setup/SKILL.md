---
name: kawarabanya-slack-setup
description: kawarabanya の Slack 側を準備する・直す（Slack アプリの作成・Bot のトークン・権限・Socket Mode・イベント・チャンネルの作成・Discord と両方に投稿する設定）。「Slack にも流したい」「Slack に移りたい」「Slack の準備をして」「Slack で @kawarabanya に反応しない」「not_in_channel / missing_scope が出る」と頼まれたら使う。Slack 一般の質問や、kawarabanya 以外のアプリには使わない。Discord 側は kawarabanya-discord-setup。
---

# kawarabanya の Slack の準備

Slack のアプリの設定（https://api.slack.com/apps）は利用者にしかできない（ブラウザで本人がログインして操作する）。手順を短く示し、終わったら知らせてもらって、こちらは CLI で確かめる。詳しい説明は README の「Slack」。

## 手順

1. **アプリ**（利用者）: 「Create New App」→「From scratch」→ 使うワークスペース。
2. **権限**（利用者）: 「OAuth & Permissions」の Bot Token Scopes に `chat:write`, `chat:write.customize`, `channels:read`, `channels:manage`, `channels:join`, `groups:read`, `groups:write`, `reactions:read`, `reactions:write`, `app_mentions:read`, `channels:history`, `groups:history` を足して「Install to Workspace」。
3. **トークン**（利用者）: Bot User OAuth Token（`xoxb-`）を `.env` の `SLACK_BOT_TOKEN=` に貼ってもらう。トークンはチャットに貼らせない・こちらで表示しない（貼られたら「OAuth & Permissions」で Revoke してもらい、作り直す）。`uv run kawarabanya doctor` の「Slack の Bot」が ✅ か確かめる。
4. **Slack から頼む**（利用者。要らなければ飛ばす）: 「Socket Mode」をオン → App-Level Token（`connections:write`、`xapp-`）を `.env` の `SLACK_APP_TOKEN=` に。「Event Subscriptions」をオンにして bot events に `app_mention`・`reaction_added`・`reaction_removed`。「Interactivity & Shortcuts」をオン（URL は空でよい）。変えたら「Install to Workspace」をやり直す。
5. **設定**: `config.toml` に `[chat] targets = ["discord", "slack"]`（Slack だけなら `["slack"]`）と `[slack] mention_user_id`（利用者のメンバー ID。プロフィール →「メンバー ID をコピー」、U で始まる）を書く。頼めるのはこの人だけ。
6. **チャンネル**: `uv run kawarabanya slack setup`（予定の表示だけ）を見せ、問題なければ `uv run kawarabanya slack setup --apply`。ジャンルごと（`kawarabanya-tech` など）・まとめ（`kawarabanya-digest`）・予定（`kawarabanya-schedule`、非公開）ができ、どれにも `mention_user_id` の人を招待する。
7. **確かめる**: `uv run kawarabanya discord test tech "テスト"`（targets のすべてに送る）→ Bot を再起動（`systemctl --user restart kawarabanya-bot`）→ Slack で「@kawarabanya いまどうなってる？」に返事が来るか。

## うまくいかないとき

| 症状 | 原因と直し方 |
|---|---|
| `missing_scope` | 2 の権限が足りない。足して「Install to Workspace」をやり直す（トークンは変わらない） |
| `not_in_channel` | Bot がチャンネルにいない。公開チャンネルは `slack setup --apply` が入る。非公開は Slack でチャンネルに Bot を招待する |
| 投稿者名がフィード名にならない | `chat:write.customize` がない（2） |
| @kawarabanya に反応しない | `SLACK_APP_TOKEN` がない・Socket Mode かイベント（4）がオフ・Bot を再起動していない・`mention_user_id` 以外の人が頼んでいる（`journalctl --user -u kawarabanya-bot -n 50`） |
| ボタンを押しても何も起きない | Interactivity がオフ（4）か、10 分を過ぎた |

## 決まり

- 実行中の DB は版 5 以降（`channels`・`posts`）。古い版（Discord だけの kawarabanya）に戻すと開けないので、戻すときは `data/kawarabanya.db.bak-v4` を使う。
- `discord delete-duplicates --apply`・`digest --delete` は Slack の投稿も消す（targets のすべて）。kawarabanya-discord-setup の決まりと同じく、利用者に実行してもらう。
