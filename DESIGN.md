# feedpipe 設計（v0.2）

最終更新: 2026-11-07（1 日のまとめ・投稿へのリンクを追加）

## 1. 目的と方針

- **個人用 Discord サーバーを管理する総合 Bot**。今ある機能は RSS（全文 Markdown 保存 → 要約 → Discord フォーラムに投稿）で、機能は今後足していく。
- 個人運用（利用者 1 人・サーバー 1 つ）なので、複数ユーザー・複数サーバーへの対応はしない。
- **軽さ優先**: SQLite + TOML + 1 プロセスの Bot。DB サーバー・Docker は使わない。LLM は CPU で動くローカルモデル。
- 骨組み（プラグイン機構など）は、使う機能が決まってから作る。

## 2. 全体構成

```
systemd --user: feedpipe-bot.service
└─ feedpipe bot（常駐。discord.py で Gateway に接続）
   ├─ 子プロセス: scripts/llama-server.sh（llama.cpp + Qwen3.5-4B、OpenAI 互換 API :8080）
   └─ 60 分ごとに子プロセス: feedpipe run → feedpipe digest --due（21 時以降に 1 日 1 回）
        取得 ─▶ 既読判定 ─▶ 全文抽出 ─▶ Markdown 保存 ─▶ サブジャンル・重要度判定 ─▶ 要約 ─▶ 投稿 ─▶ 👍 集計
        (httpx)  (SQLite)   (trafilatura) (archive/)     (LLM 1 トークン) (LLM)  (Webhook) (Bot API)

設定・データ:
  config.toml   … 動作設定（git 管理外）        .env         … Bot トークン（git 管理外）
  genres.toml   … ジャンルとフィードの割り当て（git 管理外。見本は genres.example.toml）
  data/feedpipe.db … SQLite（feeds / items / discord_channels）
  archive/      … 記事の Markdown
```

- `run` を別プロセスにするのは、要約に数十分かかっても Bot のイベントループ（Discord との接続維持）を止めないため。前回の `run` が終わるまで次は始めない。
- 投稿は Webhook、チャンネル・タグの作成と 👍 の読み取りは Bot トークン（REST）。どちらも同じ Bot（同じ招待 URL）で管理する。

### 2.1 コードの構成（`src/feedpipe/`）

```
config.py  db.py  bot.py  discord_api.py  llm.py   # 共通: 設定・SQLite・常駐 Bot・Discord REST/Webhook・LLM クライアント
rss/        # RSS 機能
  fetcher.py check.py item.py fulltext.py opml.py genres.py
  archive.py（Markdown 保存） pipeline.py（取得〜要約の流れ = run） notify.py（投稿・👍） digest.py（1 日のまとめ）
cli/        # コマンド
  __init__.py（引数の定義と main） common.py（表示の補助・DB/Discord の用意） rss.py（RSS のコマンド） server.py（Discord 管理・bot）
```

機能を足すときは、`rss/` と同じように機能ごとのパッケージを作り、コマンドは `cli/` に、Discord 上の対話は `bot.py` に足す。`discord_api.py` のフォーラム構成（ジャンル＝フォーラム）は今は RSS 専用の処理が混ざっている。

### 2.2 RSS のないサイト
RSSHub（Docker、`127.0.0.1:1200`）が出す RSS を普通のフィードとして登録できる（feedpipe 側には専用の処理を持たない）。**2026-11-07 に試したあと停止・削除した**（使うときは README の手順で起動する）。2026-11-07 時点の版で名前空間は 2,087（日本向けは約 100。Zenn・Qiita・はてな・note はない）。サイト側の変更で動かないルート（例: NHK）や、設定がないと使えないルート（例: GitHub トレンドはトークンが要る）がある。

### 2.3 HTML 取り込み（`rss/htmlsource.py`）
- `genres.toml` の `feeds` に `{ url, item, title, link, date, link_pattern }` を書くと、一覧ページから CSS セレクタで記事を取り出し、以降は RSS と同じ処理（本文・保存・判定・要約・投稿）を通す。`feeds.source_type = 'html'`、セレクタは `options.html`。
- 先行例を調べて決めたこと（2026-11-07）:
  - RSS-Bridge CssSelectorBridge・html2rss・FreshRSS と同じく「記事の塊のセレクタ＋中のタイトル・リンク・日付」。XPath より CSS（クラスの完全一致で壊れやすいという指摘）。
  - 記事を見分けるキーは、`#` 以降と追跡用パラメータ（utm_ など）を除いた URL（同じ記事の重複取り込みを防ぐ）。
  - セレクタは黙って 0 件になるので、**0 件は失敗**（not_feed）として数え、5 回で自動無効化される。
  - 本文は記事ページから trafilatura で取る（RSS-Bridge の content_selector に当たる部分は既存の仕組み）。
  - JSON-LD などからの自動検出（html2rss の AutoSource）は作っていない。
- 文字コードの指定がない HTML は lxml が latin-1 とみなすので、先に UTF-8 として読む。
- **ボット対策で断られるサイトには、ブラウザを装ってまで取りに行かない**（例: すごろくやは feedpipe の User-Agent に 403 を返す）。

## 3. 主な処理

### 3.1 フィード管理
- `import-opml`: ネストしたフォルダは `a/b` のカテゴリになる。重複 URL は最初の 1 件だけ。
- `check`: 301/308 の移転で、移転先が正常なときだけ URL を更新（`original_url` は残す）。404/410・ホスト消滅・非フィードは `--disable-dead` で無効化。それ以外の失敗は 5 回連続で自動無効化。最新記事の日時を記録し、1 年以上更新のないものを報告する。
- `genres.toml` が投稿先の正本。書かれていない有効なフィードは分類なし（投稿されない）になり、`--disable-unassigned` で取得も止める。

### 3.2 取得と保存（run）
- 条件付き GET（ETag / Last-Modified）。全体 16・同一ホスト 2 の同時接続制限。
- **初回のフィードは既存記事を既読にするだけ**（`--backfill N` で新しい順に N 件だけ保存）。フィード内の重複記事は先に除く。
- 本文は元記事から trafilatura で Markdown 抽出。取れない・短すぎる（200 字未満）ときはフィードの本文と長い方を使い、Front Matter の `source` に記録する。
- **同じ記事は 1 件にまとめる**: 保存の前に、同じ URL か同じ本文（空白を除いた SHA-1、200 字以上のときだけ）の記事が保存済みなら、保存も投稿もせず `duplicate` にする（Yahoo!ニュース・トピックスは同じ記事を「主要」と「国内」などの複数のフィードに載せる。11/07 の投稿 176 件中 9 件が重複だった）。まとめは投稿した記事から作るので、まとめにも 1 件だけ載る。
- 保存先: `archive/{カテゴリ}/{年}/{月}/{日付}_{タイトル}.md`（Front Matter: title / url / feed / category / published / fetched / source / summary / discord＝投稿へのリンク）。

### 3.3 重要度の判定と要約
- **公開から 3 日以内の記事だけ**対象にする（投稿しない古い記事に CPU を使わない）。
- **サブジャンルの判定（Jev 風）**: トップレベルのジャンルのフィードの記事は、そのジャンルのサブジャンル（`label`＋`description`）を A〜T、「どれにも当てはまらない」を Z として 1 文字で選ばせ、`items.subgenre` に記録する（1 件 約 1.5 秒）。サブジャンルの下に書いた（固定した）フィードは判定しない。判定したサブジャンルの重要度の基準を、次の判定に使う。
- **重要度の判定（Jev 風）**: 見出し＋冒頭 200 字を渡し、A（高い）/ B / C の 1 トークンだけを文法制約で出させて、A の確率を `items.priority` に記録する（1 件 約 1.5 秒）。判定基準はジャンルごとに `genres.toml` の `priority` で変えられる。ラベルの割合は偏る（tech では A が 7 割）ので、ラベルではなく確率で選ぶ。
- **A の確率が 0.8 以上の記事だけ要約する**（`priority_threshold`。0 なら全件）。tech の 1 日分では約 3 割。判定に失敗したときは取りこぼさないよう要約する。
- 要約: 本文の先頭 1,000 字を渡し、2〜3 文・120 字程度で要約させる（1 件 約 9 秒）。思考モードは切る。日本語と英数字の間の余分な空白は除く。
- 要約しなかった記事も、本文の先頭 200 字で投稿する（👍 で好みを集めるため）。
- サーバーに繋がらなければ、その回は判定・要約なしで続ける（後から `summarize` で補える）。

### 3.4 Discord
- **ジャンル 1 つ = フォーラム 1 つ**（親カテゴリ「feedpipe」の下。年齢制限オン）。**フォーラムのタグはサブジャンルだけ**（上限 20 個、名前は 20 字）。フィード名はタグにせず、投稿者名で示す（フィードが 20 を超えるとタグが足りなくなるため）。
- 投稿のタグは記事のサブジャンル（`items.subgenre`、なければフィードを固定したサブジャンル）。
- **1 記事 = 1 投稿**: スレッド名 = 記事タイトル、表示名 = フィード名、本文 = 要約（なければ本文の先頭 200 字）＋公開日時＋URL。メンションは無効化。
- 公開から 3 日を過ぎた未通知の記事は投稿せず `stale` にする。1 回の実行で投稿するのは 30 件まで。
- `discord setup` はジャンル構成に合わせてフォーラム・タグ・Webhook・既定リアクションを揃える（何度実行しても同じ結果）。`--prune` で対応しないフォーラムを削除する。
- 投稿へのリンク（`https://discord.com/channels/{サーバー}/{スレッド}`）は、Markdown の Front Matter（`discord`）、1 日のまとめ、`interests` に載せる。
- **👍 は「興味あり」の印**として集計するだけで、投稿の取捨選択には使わない（押されていない投稿は「見ていない」可能性があるので、興味なしとは扱わない）。投稿から 7 日以内の記事を 6 時間ごとに確認する。

### 3.5 1 日のまとめ（digest）
- まとめ用のテキストチャンネル（`[digest] channel`、既定 **#ニュース**。サーバー内を名前で探し、feedpipe 配下でなくてよい。なければ作らずに知らせる）に、**ジャンルごとに 1 メッセージ**「## 📰 テック MM/DD のまとめ（N 件）」（見出しは genres.toml の label）を 1 日 1 回投稿する（`[digest] hour` 以降の最初の定期実行で。投稿済みの日は `digests` テーブルで飛ばす）。リンク先は各ジャンルのフォーラムの投稿。
- 本文 = LLM が直近 24 時間の見出しから書く「今日の傾向」（2 文）＋ **重要度の高い順の投稿へのリンク**（上位 10 件、👍 の数付き）＋「ほか N 件」。2,000 字に収める。
- 「主な話題」を LLM に箇条書きさせる案は、関係のない話題を 1 行に混ぜたので採らず、一覧はコードで作る。
- run の後に続けて実行する（要約サーバーを run と取り合わないため）。
- 投稿したメッセージの ID を `digests` に残し、同じ Webhook で書き換え（`--update`、今日の分だけ）・削除（`--delete`、記録も消す）ができる。まとめの投稿先を別のチャンネルに替えると、それより前のまとめは書き換え・削除できない（Webhook は自分が投稿したメッセージしか操作できない）。

## 4. データ（SQLite）

| テーブル | 主な列 |
|---|---|
| `feeds` | url（移転で更新）/ original_url / title / category（= ジャンルのパス）/ enabled / disabled_reason / etag / last_modified / last_status / fail_count / latest_entry_at / initialized_at / options（JSON） |
| `items` | feed_id / key（guid か link）/ url / title / published / status / md_path / summary / priority（重要度 = A の確率）/ subgenre（LLM が判定したサブジャンル）/ content_hash（重複判定用の本文のハッシュ）/ notified_at / discord_thread_id / likes / likes_checked_at |
| `discord_channels` | category（トップレベルのジャンル。まとめの投稿先は `#digest`）/ guild_id / channel_id / webhook_url / tags（JSON: `feed:<id>` と `sub:<パス>` → タグ ID） |
| `digests` | genre / day（ローカル日付）/ thread_id（投稿したメッセージの ID）— 投稿済みのまとめ |

`items.status`: `seen`（初回の既読化）→ `new` → `saved` → `notified`。投稿しなかったものは `stale`、同じ記事が保存済みだったものは `duplicate`（error に元の記事の id）、失敗は `error`。状態を記録するので、途中で落ちても次の実行で続きから処理する。列の追加は `db.ADDED_COLUMNS` で自動的に行う。

## 5. 技術選定

| 用途 | 採用 | 理由 |
|---|---|---|
| 言語・管理 | Python 3.12 + uv | trafilatura を使うため |
| フィード解析 | feedparser | RDF を含む古い形式に対応 |
| HTTP | httpx（asyncio） | 並列取得・同時接続制限・条件付き GET |
| 本文抽出 | trafilatura | 日本語の精度が高く、Markdown を直接出せる |
| Discord | discord.py（常駐）＋ httpx（REST・Webhook） | |
| LLM | llama.cpp `llama-server` + **Qwen3.5-4B** Q4_K_M | 下の比較による。OpenAI 互換なので他の API にも替えられる |
| 設定・DB | TOML（tomllib）・SQLite（sqlite3） | 標準ライブラリ |

### 要約モデルの比較（tech 8 記事・CPU 8 スレッド）

| | LFM2.5-1.2B-JP | Qwen3-4B-Instruct-2507 | Qwen3.5-4B |
|---|---|---|---|
| メモリ | 約 1.4GB | 約 6.1GB | 約 6.0GB |
| 正確さ | ✕ 本文にない日付を作った | ○ | ○ |
| 全体の傾向 | ✕ 1 件目の言い換え | ○ | ◎ |

LFM（軽さ優先で最初に選んだ）は事実の誤りがあったため、Qwen3.5-4B に変更した。

## 6. 実測値（2026-11-07）

| 項目 | 値 |
|---|---|
| feedly.opml | 333 フィード → つながる 189 → 1 年以内に更新あり 103 → 重複を除いて **94 件をジャンルに割り当て** |
| 記事の量 | tech だけで 1 日約 120 件。news は重複フィード 9 件を外して約 160 件/日（外す前は 500 件超） |
| Qwen3.5-4B の速さ（CPU） | 読み込み約 130 トークン/秒、出力約 14 トークン/秒 |
| 要約 1 件 | 入力 3,000 字で約 16 秒 → **1,000 字で約 9 秒**（品質はほぼ同じ） |
| 重要度の判定 1 件 | **約 1.45 秒**（入力 約 240 トークン、出力 1 トークン） |
| tech 1 日分（118 件）の判定 | A の確率 0.9 以上 23 件 / **0.8 以上 36 件** / 0.7 以上 51 件。上位は大きな事件、下位はアクセサリー・グッズ紹介 |
| 1 日 400 件の CPU 時間 | 全件を 3,000 字で要約: 約 107 分 → **全件判定＋上位 3 割を 1,000 字で要約: 約 28 分** |
| 似た見出しの記事をまとめる | 118 件 → 109 話題（8% 減）。効果が小さいので採用しない |

## 7. 今後の候補・未決事項

1. **まとめの「今日の傾向」**: 見出しだけから書くので、芸能などの話題に引っ張られることがある（ジャンルごとの判定基準を書けば一覧の並びは改善する）。
2. **重要度の基準の調整**: 0.8 は tech の 1 日分で決めた仮の値。👍 がたまったら、👍 の付いた記事の priority と照らして調整する。tech 以外のジャンルの判定基準も書く。
3. **llama-server のスレッド数**: 8 → 16 で読み込みが速くなるかは未測定。
4. **スラッシュコマンド**: `/feed add` など。次に作る機能が決まってから。
5. **WSL2 での常駐**: Windows が起動している間だけ動く。WSL の起動時に自動で立ち上げるには `loginctl enable-linger` が必要。
6. **プロジェクト名**: Bot 全体の名前にするか（今は `feedpipe`）。
7. サブジャンルの判定の精度（実データ 14 件の目視では妥当。どれにも当てはまらない記事はタグなし）。
