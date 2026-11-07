---
name: kawarabanya-install
description: kawarabanya をこのマシンに入れる・動かない環境を直す。「インストールして」「セットアップして」「clone したけど何すればいい？」「要約のモデルも入れたい」「uv sync でこける」「doctor が ❌」「Bot が起動しない」と頼まれたとき、またはリポジトリを複製した直後に、はっきり言われなくても使う。Discord 側の準備は kawarabanya-discord-setup、日々の操作は kawarabanya-ops。
---

# kawarabanya のインストール

リポジトリのルートで作業する。手順の正本は README.md の「クイックスタート」。ここでは進め方と決まりだけを書く。

## 進め方

1. 前提を確かめる: `uv --version`（なければ `curl -LsSf https://astral.sh/uv/install.sh | sh` を勧め、実行は利用者に任せる）。Linux / WSL2 であること。
2. 要約を使うかを利用者に聞く（CPU 8 スレッド・メモリ 6 GB・ディスク 3 GB 程度が要る。なくても取り込み・投稿はできる）。
   - 使う: `scripts/install.sh --llm`（llama.cpp と Qwen3.5-4B 約 2.7 GB をダウンロードする。時間がかかるのでバックグラウンドで実行し、終わりを待つ）
   - 使わない: `scripts/install.sh`
   - `uv sync` で失敗したら、エラーの最後の数行を読む。ネットワーク（プロキシ・DNS）か、Python 3.12 が取れないことが多い（`uv python install 3.12` を試す）。
3. 最後に出る `kawarabanya doctor` の結果を、❌ → ⚠️ の順に 1 つずつ直す（下の表）。直したら `uv run kawarabanya doctor` をもう一度実行し、❌ が 0 になるまで続ける。
4. Discord の準備がまだなら kawarabanya-discord-setup に進む。常駐は Discord の準備のあと `scripts/install.sh --service`。

## doctor の結果ごとの直し方

| 表示 | 直し方 |
|---|---|
| ❌ genres.toml がない / 読めない | `cp genres.example.toml genres.toml`。読めないときはエラーの行を直す（同じ URL が 2 か所・不明なキー・壊れた TOML） |
| ❌ Discord: トークンがない | kawarabanya-discord-setup の 1〜2 |
| ❌ Discord のサーバー | Bot を招待する（`uv run kawarabanya discord invite-url`）。複数なら `config.toml` の `[discord] guild_id` |
| ❌ DB | `data/` に書き込めるか。「新しい版」と出たら `git pull && uv sync` で kawarabanya を新しくする（DB を消さない） |
| ❌ 要約サーバーの起動コマンド | `scripts/install.sh --llm` をやり直す。パスを変えているなら `scripts/llama-server.sh` の `LLAMA_DIR` / `LLAMA_MODEL` |
| ⚠️ MESSAGE CONTENT INTENT がオフ | Developer Portal の Bot の設定でオン（Discord から日本語で頼むときに要る） |
| ⚠️ 要約サーバーに応答がない | Bot を起動すれば一緒に起動する（`llm_server_command`）。単体なら `scripts/llama-server.sh` |
| ⚠️ フォーラムがない | `uv run kawarabanya discord setup --apply`（kawarabanya-discord-setup の 5） |
| ⚠️ 常駐サービス | `scripts/install.sh --service` |
| ❌ RSSHub に応答がない | `docker start rsshub`（コンテナがなければ README の「RSS のないサイト」の `docker run`）。Docker Desktop が起動しているか、サインイン時に自動で起動する設定か |

## 決まり

- `.env`・`config.toml`・`genres.toml` は利用者が書いた設定で、消すと作り直せない（購読リストやトークン）。上書き・削除しない（`install.sh` もあるものはそのまま）。
- トークンの値を表示・ログ・コミットに出さない（会話や履歴に残ると、Bot を乗っ取れる）。
- `sudo` が要る操作（`apt install libgomp1`・`loginctl enable-linger`）は、パスワードの入力が要り、システム全体を変えるので、コマンドを示して利用者に実行してもらう。
- `--service` は既存の `~/.config/systemd/user/kawarabanya-bot.service` を書き換える。動いている Bot があるなら、そのことを伝えてから実行する。
