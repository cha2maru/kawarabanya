# 変更の記録

版の付け方: `0.x` の間は開発途中で、設定・コマンド・DB の形が予告なく変わります（DB は起動時に自動でマイグレーションし、前にバックアップを取ります）。

## 未リリース

- Slack から頼める（Socket Mode。`@kawarabanya` への依頼・確認ボタン・👍・👎 をその場で数える。`SLACK_APP_TOKEN`）。`targets = ["slack"]` なら Discord にはつながずに動く
- Slack にも投稿できる（`[chat] targets = ["discord", "slack"]`・`[slack]`・`slack setup`）。ジャンルごとのチャンネルに 1 記事 = 1 枚のカード（Block Kit。見出し・要約・サブジャンル・公開日時・元記事のボタン）、Bot が 👍・👎 を先に付けておく。まとめ・予定・不調のお知らせも Slack に送る
- 投稿先を差し替えられる形にした（Discord と Slack の両対応の準備。`[chat] targets`）。DB は版 5（`discord_channels` → `channels`、`items.discord_thread_id` → `posts`、`digests` にサービス）。起動時に自動で上がる（前に `data/kawarabanya.db.bak-v4` にバックアップを取る）
- フォーラムの投稿が 1 日（これまで 3 日）で閉じるようにし（`discord setup --apply` で揃える）、それより前の投稿を閉じる `discord archive` を足す。サーバー全体で開いているスレッドの上限 1,000 件に達して投稿できなくなっていた
- 要約サーバーのメモリを 9 GB → 4.6 GB に減らす（`scripts/llama-server.sh` で llama-server のプロンプトのキャッシュ `--cache-ram` を切る。`LLAMA_CACHE_RAM` で戻せる）
- 👍・👎 から好みを学び、まとめの並びに反映する（ジャンル・サブジャンル・見出しと冒頭の語・サイトごと。`[digest] pref_max`・`site_pref_max`・`pref_half_life_days`、`interests` で表示）。上位の最後の 1 枠は好みを除いた点で選ぶ。DB は版 4（`items.dislikes`）
- フィードの URL の `{days_ago:N}` を取得のたびに N 日前の日付にする（RSSHub の GitHub 検索で「直近 7 日に作られた人気リポジトリ」など）
- 好みの語の見直し（`prefs review`・`prefs ignore`）。「二回」のような漢数字＋助数詞と、数字の直後から始まる漢字は語として取らない
- 👍・👎 を Bot が付いたその場で数える（これまでは run で 6 時間ごと、投稿から 7 日以内だけ）

## v0.2.1（2026-11-08）

- `[discord] mention_user_id` で自分にメンションする: 予定の投稿はすべて、まとめは投稿し終えたときに 1 通だけ

## v0.2.0（2026-11-08）

RSS に続く 2 つ目の機能として、予定（Google カレンダー）の通知を足した版（プレリリース）。DB は起動時に版 1 → 3 へ自動で上がる（前に `data/kawarabanya.db.bak-v1`・`bak-v2` にバックアップを取る）。

### 新しい機能

- 予定（Google カレンダー）の通知: 今日と明日の予定・開始 30 分前・予定の追加や変更を `#予定` に投稿する（`schedule login`・`schedule check`、`[schedule]`、gcsa で読み取りだけ）。`discord setup --apply` が `#予定` を、持ち主と Bot だけが見られる非公開のチャンネルとして作る。DB は版 3（`events`）
- 日本語以外の記事は見出しを訳し（スレッド名は「訳（原題）」）、重要度にかかわらず日本語で要約する。全文訳は `items translate`（Discord では投稿のスレッドで「翻訳して」）。DB は版 2（`items.title_ja`）
- まとめの並びに話題になり方を足す（同じ出来事を報じたサイトの数、Google トレンドの急上昇ワード。`[digest] trends_geo`）
- 語とジャンルの紐づけ（`genres keyword`・genres.toml の `keywords`、「@Bot Cloudflareはtech」）。見出しか冒頭にある語で記事のジャンルを決める
- 記事を別のジャンルに移す（`items move`、記事の投稿のスレッドで「@Bot これは映画」）。別のジャンルへはサブジャンルなしで移し、`genres suggest` が新しいサブジャンルの材料にする
- 不調を Discord に知らせる（自動で止めたフィード・RSSHub の無応答・定期処理の 3 回連続の失敗）
- まとめの「傾向」の文をやめ、話題（最大 3 つ）の箇条書きにする（別々の話題を 1 つの文につなげていた）
- `[general] rsshub_url` を書くと、`doctor` が RSSHub が動いているかを確かめる
- Bot の招待の権限に「スレッドでメッセージを送る」を足す

### 直した不具合

- Bot の起動直後、モデルの読み込みを待たずに取り込んで、判定も要約もなしで投稿していた
- `<?xml encoding=...?>` で始まるページの記事が保存できなかった
- `run` が記事単位の失敗で終了コード 1 になっていた（Bot が不調として数えないように）
- RSSHub のフィードの登録で `--no-discover` を勧めていた（自動検出で名前も付く）

## v0.1.0（2026-11-07）

最初の公開版（プレリリース）。

### できること

- RSS / Atom の取り込み、OPML の取り込み・書き出し（取り込んだサイトは genres.toml にも書き足す）、サイトの URL からの RSS の自動検出
- RSS のないサイト: CSS セレクタでの HTML 取り込み、サイトマップ、サイト別の拡張（`extensions/<名前>.py`）、RSSHub で取れるかの確認
- 元記事の本文を Markdown で保存（文字コードの自動判別、保存は UTF-8）
- ローカル LLM（Qwen3.5-4B・llama.cpp・CPU）で、記事の全サブジャンルへの振り分け・重要度の判定・要約・同じ出来事の判定
- ジャンルごとの Discord フォーラムへの投稿（サブジャンルがタグ）、6:30・18:30 のまとめ、👍 の集計
- 好き嫌い（like / down / low / mute）をジャンル・サイトごとに
- 当てはまるサブジャンルのない記事から、新しいサブジャンルを LLM が作る（多すぎれば似たものをまとめる）
- Discord から日本語で頼める（メンション → コマンドに直してボタンで確認）
- `scripts/install.sh`・`kawarabanya doctor`・DB のマイグレーション・Claude Code のスキル（インストール・Discord の準備・日々の操作）

### 分かっている制限

- 試したのは Ubuntu / WSL2 と、個人の Discord サーバー 1 つだけ
- まとめの「傾向」の文が、別々の話題を 1 つの文につなげることがある（未リリースで修正）
- 話題の広いサイトは、`--genre auto` でジャンルが決まらない
- 未決事項は DESIGN.md の §7
