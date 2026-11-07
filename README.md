# kawarabanya（瓦版屋）

個人用 Discord サーバーを管理する Bot。今ある機能は RSS リーダー: 登録したサイトの新着記事を全文 Markdown で保存し、**CPU で動くローカル LLM** で振り分け・要約して、ジャンルごとの Discord フォーラムに投稿する。

- **全文を Markdown で保存**: 元記事のページから本文を取り出し、`archive/` に残す（文字コードは自動で判別し、UTF-8 で保存）。
- **ローカル LLM で整理**: 記事をサブジャンルに振り分け（フォーラムのタグになる）、重要度を判定して、重要な記事だけ要約する。外部の API は使わない（Qwen3.5-4B を llama.cpp で動かす。1 件 1.5〜9 秒）。
- **重複をまとめる**: 同じ URL・同じ本文の記事に加え、本文の違う「同じ出来事」の記事も LLM で見分けて 1 件にする。
- **まとめ**: 1 日 2 回（6:30・18:30）、ジャンルごとの話題（最大 3 つ）と重要な記事へのリンクを投稿する。
- **好みを覚える**: 投稿に付いた 👍 を数える。ジャンルの好き嫌い（好き・うすい・ミュート）でまとめの並びや投稿を変えられる。
- **ジャンルが育つ**: どのサブジャンルにも当てはまらない記事がたまると、LLM が新しいサブジャンルを作る（多すぎれば似たものをまとめる）。
- **Discord から日本語で頼める**: 「@Bot このサイトを追加して」「スポーツはまとめに出さなくていい」などをコマンドに直し、ボタンで確認して実行する。
- **RSS のないサイト**: CSS セレクタでの HTML 取り込み、サイトマップ、サイト別の拡張（Python）。RSSHub で取れるかも調べられる。

設計と実測値は [DESIGN.md](DESIGN.md)、コードの書き方は [docs/CODING.md](docs/CODING.md)。

> [!WARNING]
> **開発途中のツールです。** 手順・設定・DB の形は予告なく変わります。また、作者の環境（Ubuntu / WSL2・個人の Discord サーバー 1 つ）でしか試していません。
> **インストールから設定まで、[Claude Code](https://claude.com/claude-code) などの AI コーディングエージェントの手助けを受けながら進めてください。** このリポジトリには、手順と注意点をまとめた Claude Code のスキル（`.claude/skills/`）があります。リポジトリを複製したフォルダで Claude Code を起動し、「インストールして」「Discord の準備をして」と頼むと、それに沿って進めます。下の手順は、その中身を人が読める形にしたものです。

## 必要なもの

- Linux（Ubuntu・WSL2 で動作を確認）と [uv](https://docs.astral.sh/uv/)（Python 3.12 は uv が用意する）
- Discord のサーバー（「コミュニティ」を有効にする。フォーラムを作るのに必要）と、Bot のアプリケーション
- 要約に使う場合: CPU 8 スレッド程度・メモリ 6 GB 程度・ディスク 3 GB（llama.cpp と Qwen3.5-4B Q4_K_M）。なくても取り込み・保存・投稿はできる

## クイックスタート

### 1. インストール

Claude Code を使う場合は、複製したフォルダで `claude` を起動して「インストールして」と頼めば、以下の 1〜5 を順に案内する。

```sh
git clone <このリポジトリ> kawarabanya && cd kawarabanya
scripts/install.sh --llm      # 依存・設定の見本・llama.cpp と要約モデル（約 2.7 GB）。要約を使わないなら --llm なし
```

`config.toml`・`genres.toml`・`.env` は見本（`*.example*`）からコピーされる（あれば上書きしない）。最後に `kawarabanya doctor` が足りないものを表示する。

### 2. Discord の Bot を作る

1. [Discord Developer Portal](https://discord.com/developers/applications) で **New Application** を作る。
2. 左の **Bot** → **Reset Token** でトークンを出し、`.env` の `DISCORD_BOT_TOKEN=` に書く。
3. 同じ **Bot** の画面の **Privileged Gateway Intents** で **MESSAGE CONTENT INTENT** をオンにして保存する（Discord から日本語で頼むときに使う）。
4. Bot をサーバーに招待する:
   ```sh
   uv run kawarabanya discord invite-url     # 表示された URL をブラウザで開き、サーバーを選ぶ
   ```
5. サーバーの設定で **コミュニティ** を有効にし、まとめを投稿するテキストチャンネル（既定 `#ニュース`）を作っておく。

### 3. フィードとジャンル

```sh
uv run kawarabanya import-opml subscriptions.opml --genre auto   # 今使っている RSS リーダーの OPML を取り込む（ジャンルは LLM が決める。試すだけなら sample.opml）
uv run kawarabanya feeds add https://example.com/ --genre tech   # 1 件ずつなら、サイトの URL から RSS を探して登録
uv run kawarabanya check --disable-dead             # つながらないフィードを無効にする
$EDITOR genres.toml                                 # ジャンル・サブジャンルとフィードの URL を書く（書き方は下の「設定」）
uv run kawarabanya genres apply --disable-unassigned
```

### 4. Discord のフォーラムを作って、試す

```sh
uv run kawarabanya discord setup            # 作るフォーラム・タグの予定を表示
uv run kawarabanya discord setup --apply    # ジャンルごとのフォーラムとタグ、Webhook を作る
uv run kawarabanya run                      # 取り込んで投稿する（初回のフィードは既存の記事を既読にするだけ）
uv run kawarabanya digest                   # まとめを今すぐ投稿する
uv run kawarabanya doctor                   # すべて ✅ になっているか
```

### 5. 常駐させる

```sh
scripts/install.sh --service                # systemd のユーザーサービス kawarabanya-bot を登録して起動
journalctl --user -u kawarabanya-bot -f     # ログ
sudo loginctl enable-linger $USER           # ログインしていない間も動かす場合
```

Bot は 60 分ごとと 6:30・18:30 に、取り込み（`run`）→ サブジャンルの作成（`genres suggest`）→ まとめ（`digest`）を順に実行する。`config.toml` の `[bot] llm_server_command` を書いておくと、要約サーバーも Bot と一緒に起動・停止する（`install.sh --llm` が書く）。WSL2 では Windows が起動している間だけ動く。

## 使い方

### Discord から頼む

Bot にメンションして日本語で書くと、ローカル LLM がコマンドに直す（サーバーの持ち主だけ）。Developer Portal の Bot の設定で **MESSAGE CONTENT INTENT** をオンにしておく（「@Bot」は Bot のロールへのメンションになることがあり、そのときは本文を読む許可が要る）。サイトの追加・削除、好き嫌いの変更、取り込み・まとめの実行は、ボタンで確認してから実行する。状態や一覧はすぐ返す。

```
@Bot https://example.com/news をボードゲームに追加して   → feeds add … --genre hobby/boardgame（確認）
@Bot GIGAZINE を外して                                    → feeds remove 182（確認）
@Bot スポーツはまとめに出さなくていい                     → genres interest culture/sports low（確認）
@Bot ボドゲーマのレビューは優先度下げて                  → feeds option 139 interest down（確認）
@Bot（OPML を添付して）これ取り込んで                     → import-opml … --genre auto（確認）
@Bot OPML ちょうだい                                       → export-opml（OPML ファイルを添付して返す）
@Bot Cloudflareはtech                                     → genres keyword Cloudflare tech（確認）
@Bot これは映画（記事の投稿のスレッドで）                 → items move 123 culture/movie（確認）
@Bot Cloudflareはtech（記事の投稿のスレッドで）           → items move 123 tech --keyword Cloudflare（確認）
@Bot いまどうなってる？                                   → status
@Bot 何ができるの？                                       → 例の一覧
```

### コマンド

| コマンド | 内容 |
|---|---|
| `bot` | 常駐 Bot。Bot にメンションして日本語で頼むと、コマンドに直して返事をする（「Discord から頼む」）。60 分ごとと、まとめの時刻（6:30・18:30）に `run`・`genres suggest --apply --due`・`digest --due` を順に別プロセスで実行する |
| `digest [--genre X] [--due] [--dry-run]` | ジャンルごとのまとめ（前回のまとめ以降の記事の話題＋重要度順の投稿へのリンク）を今すぐ #ニュース に投稿する。Bot は 6:30・18:30 に投稿する |
| `digest --update [--genre X]` | 最後に投稿したまとめを、最新の記事・重要度で作り直して書き換える |
| `digest --delete [--genre X] [--day YYYY-MM-DD]` | ジャンルごとに最後のまとめ（`--day` ならその日のすべて）を削除する |
| `run [--category X] [--backfill N] [--no-fulltext] [--no-summary] [--no-notify]` | 取得 → 保存 → 重要度の判定 → 要約 → 投稿 → 👍 集計。初回のフィードは既存記事を既読にするだけ |
| `summarize` | 保存済み・未投稿で要約のない最近の記事を、重要度を判定してから要約する |
| `notify [--category X] [--limit N] [--dry-run]` | 保存済み・未投稿の記事を投稿する |
| `likes` / `interests [--days 30]` | 👍 の数を取得する / ジャンル・フィード別の 👍 の割合と、最近 👍 した記事（投稿へのリンク付き）を表示する |
| `import-opml FILE [--genre G\|auto]` / `export-opml FILE [--all]` | OPML の取り込み・書き出し。取り込んだフィードは genres.toml にも書き足す（ジャンルは `--genre`、なければ OPML のフォルダ名と同じジャンル、`auto` ならフォルダ名で決まらないものを LLM が最近の見出しから決める） |
| `check [--disable-dead]` | 死活チェック（移転していれば URL を更新） |
| `feeds add URL [--genre G\|auto] [--title T] [--no-discover]` | サイトかフィードの URL から登録する（`--genre auto` なら LLM がジャンルを決める）。ページ内の案内（`<link rel="alternate">`）やよくある場所（/feed など）から RSS を探し、genres.toml の `G` にも書き足す。初回は既存の記事を既読にするだけ |
| `feeds rsshub URL` | URL が RSSHub のどのルートで取れるかを調べる（RSSHub の radar ルールを jsDelivr から取って `data/` に 7 日置く。RSSHub は動かさなくてよい）。`feeds add` でフィードが見つからないときも案内する |
| `feeds remove ID\|URL` | 無効にして genres.toml から外す（保存済みの記事と投稿は残す。`feeds add` で同じ URL を足すと有効に戻る） |
| `feeds list\|enable\|disable\|option` | フィードの管理（例: `feeds option ID no_fulltext true`） |
| `genres interest G like\|normal\|down\|low\|mute` | ジャンル・サブジャンルの好き嫌いを genres.toml に書く。サイトごとには `feeds option ID interest down`（値を省くと戻る。サイトの設定がジャンルより優先） |
| `doctor` | 動かすのに必要なもの（設定・DB・Discord・MESSAGE CONTENT INTENT・要約サーバー・常駐サービス）がそろっているかを調べる。❌ があれば終了コード 1 |
| `status` | 最後の取り込み・次のまとめ・要約サーバー・失敗中のフィード・記事の件数 |
| `genres keyword 語 G` | タイトルにこの語がある記事の振り分けの候補を、ジャンル `G` のサブジャンルに絞る（genres.toml の `keywords` に書く）。全角・半角と大文字・小文字は区別しない。作品名やブランド名のような固有名詞に使う |
| `items move ID G [--keyword 語]` | 記事を別のジャンルに移す。同じジャンルの中ならタグを付け替え、別のジャンルなら新しいフォーラムに投稿して元の投稿を消す（URL は変わり、👍 は引き継げない。「スレッドの管理」が必要） |
| `genres list` / `genres apply [--dry-run] [--disable-unassigned]` | ジャンルの一覧 / genres.toml の反映 |
| `genres suggest [--apply] [--days 7] [--due]` | どのサブジャンルにも当てはまらなかった記事から、新しいサブジャンルを LLM に決めさせる（案は振り分け直して確かめる）。タグの上限（20）を超えるジャンルは似たサブジャンルを先にまとめる。`--apply` で genres.toml に反映し、Discord のタグを揃えて付け直す（Bot はまとめの時刻ごとに自動で実行する） |
| `discord setup [--apply] [--prune]` / `channels` / `test GENRE` / `guilds` / `invite-url` | フォーラム・タグ・Webhook の管理 |
| `discord retag` | 投稿済みの記事のタグをサブジャンルに付け直す（未判定なら LLM で判定。Bot に「スレッドの管理」が必要） |
| `discord delete-duplicates [--apply]` | 重複（同じ URL・同じ本文）と判定した記事のフォーラムの投稿を消す（なしは一覧だけ。「スレッドの管理」が必要） |

全体のオプション: `-c config.toml`、`--db PATH`、`-v`（詳しいログ）。

## 設定

### genres.toml（git 管理外。見本: genres.example.toml）

```toml
[tech]                       # ジャンル = Discord のフォーラム 1 つ
label = "テック"
description = "IT・ガジェット・開発"
feeds = [
  "https://gigazine.net/news/rss_2.0/",  # GIGAZINE
]

priority = "A: 高い（…） B: 普通（…） C: 低い（…）"   # 重要度の判定基準（省略可）

[tech.ai]                    # サブジャンル = フォーラムのタグ。記事ごとにローカル LLM が全サブジャンルから振り分ける
label = "AI"
description = "生成 AI・機械学習・AI サービス"   # LLM が判定する手がかり
keywords = ["ChatGPT"]       # タイトルにこの語があれば、振り分けの候補をこのジャンルに絞る（`genres keyword` でも足せる）

[hobby.boardgame]
label = "ボードゲーム"
feeds = [ ... ]              # サブジャンルの下に書いたフィードは、判定せずにこのサブジャンルになる

[culture.sports]
label = "スポーツ"
interest = "low"             # 好き嫌い: like（まとめの上位に寄せる）/ down（まとめの下の方に）/ low（投稿はするが、まとめに載せない）/ mute（投稿もしない）
                             # 書かなければ普通。サブジャンルに書かなければジャンルの値を受け継ぐ（digest = false は low の古い書き方）
```

### RSS のないサイト

RSS のないサイトは、`feeds` に URL の代わりにテーブルを書くと一覧ページから記事を取り込む（HTML 取り込み）:

```toml
feeds = [
  { url = "https://www.engames-s.com/", name = "エンゲームズ（お知らせ）",
    item = ".whatnew_list li.list_item_cell",   # 記事 1 件の塊（必須。CSS セレクタ）
    title = ".list_subject",                     # 省略するとリンクの文字列
    date = "time",                               # datetime 属性か「2026年11月3日」などの文字列。省略可
    link_pattern = "/news-detail/" },            # この正規表現に合う URL だけ。省略可（link で <a> も指定できる）
]
```

`genres apply` で登録される。セレクタに一致する記事が 0 件になると失敗として数える（サイトの作りが変わったことに気づけるように）。

CSS セレクタで取れないサイト（JSON の API、特殊な作りのページ）は、**拡張**を書く。`extensions/<名前>.py`（`[general] extensions_dir`）に `parse` を書き、`feeds` に `extension = "<名前>"` を足す（それ以外のキーは拡張の設定として `options` に渡る）:

```python
# extensions/example.py
import json

def parse(content: bytes, url: str, options: dict) -> list[dict]:
    """取得したページ（bytes）から記事を返す。形は feedparser の entry と同じ。"""
    return [{"id": x["url"], "link": x["url"], "title": x["title"], "published_parsed": None, "summary": ""}
            for x in json.loads(content)[options["key"]]]

def validate(options: dict) -> None:   # 省略可。設定が正しくなければ ValueError
    if "key" not in options:
        raise ValueError("key が必要です")

def title(content: bytes) -> str | None:   # 省略可。フィードの名前（省略時はページの <title>）
    return "Example"
```

```toml
feeds = [{ url = "https://example.com/api/news", extension = "example", name = "Example", key = "items" }]
```

取得（条件付き GET・リダイレクト・失敗の回数）は本体が行い、拡張は取り出すだけ。何度も取得が要るサイトは、`parse` の代わりに `async def fetch(fetcher, url, options) -> list[dict]` を書く（`fetcher.get(url)` で取得する）。

**サイトマップ**（組み込みの拡張 `sitemap`）: RSS も一覧ページもないサイトは、サイトマップの新しい URL を記事にできる。目次（sitemapindex）なら更新の新しい子を `children` 個（既定 3）まで読み、`link_pattern` で記事のページに絞る。タイトルは Google ニュース用サイトマップの `news:title`、なければ記事ページの `<title>`。

```toml
feeds = [{ url = "https://arclightgames.jp/sitemap.xml", extension = "sitemap", name = "アークライト", link_pattern = "/product/[^/]+/$" }]
```

拡張が例外を出したり 0 件を返したりすると、そのフィードの失敗として数える。組み込みの HTML 取り込みは `extension = "html"`（省略時）。

RSSHub で取れるサイトは、[RSSHub](https://docs.rsshub.app/) を動かして普通のフィードとして登録できる（`feeds rsshub URL` で取れるかを調べられる）:

```sh
docker run -d --name rsshub --restart unless-stopped -p 127.0.0.1:1200:1200 diygod/rsshub
uv run kawarabanya feeds add "http://127.0.0.1:1200/famitsu/category/news" --genre game
```

`--restart unless-stopped` を付けると、Docker が起動するたびに RSSHub も立ち上がる（WSL2 で Docker Desktop を使うなら、Docker Desktop の Settings → General の「Start Docker Desktop when you sign in to your computer」もオンにする）。`config.toml` の `[general] rsshub_url = "http://127.0.0.1:1200"` を書くと、`doctor` が RSSHub が動いているかを確かめる（止まっていると RSSHub 経由のフィードが取得に失敗し続け、5 回で自動的に無効になる）。

フィードを移すには URL の行を別のジャンルに移して `genres apply` → `discord setup --apply`。書かれていないフィードの記事は投稿されない（保存はする）。

保存した Markdown の Front Matter には、投稿後に Discord の投稿へのリンク（`discord:`）が入る。

### config.toml（git 管理外。すべて省略可）

すべての設定と既定値・説明は見本の [config.example.toml](config.example.toml) にある。コピーして、変えたいところだけ残す。よく変えるもの:

```toml
[summarize]
priority_threshold = 0.8     # 重要度（A の確率）がこれ以上の記事だけ要約する。0 なら全件

[digest]
times = ["06:30", "18:30"]   # まとめを投稿する時刻

[bot]
llm_server_command = "scripts/llama-server.sh"   # Bot と一緒に要約サーバーを起動・停止する
```

## 運用

- **更新**: `git pull && uv sync` のあと `systemctl --user restart kawarabanya-bot`。DB の形が変わる版では、起動時に自動でマイグレーションする（その前に `data/kawarabanya.db.bak-v<旧版>` にバックアップを取る）。新しい版で作った DB を古い版で開こうとすると止まる。
- **バックアップ**: `data/`（DB）と `archive/`（記事の Markdown）、`config.toml`・`genres.toml`・`.env`。`genres.toml` は Bot も書き換える（サブジャンルの自動作成・Discord からの依頼）。
- **不調の通知**: Bot は、失敗が続いて自動で止めたフィード、RSSHub の無応答（`rsshub_url` を書いたとき）、定期処理の 3 回連続の失敗を、まとめの投稿先のチャンネルに知らせる（新しく起きたときに 1 回だけ。直ったあと再発すればまた知らせる）。
- **調子が悪いとき**: `uv run kawarabanya doctor` と `uv run kawarabanya status`、ログ（`journalctl --user -u kawarabanya-bot`）。

## 開発

```sh
uv run pytest
uvx ruff check src tests                           # 規則は pyproject.toml の [tool.ruff]。push のたびに GitHub Actions でも実行する
uv run --with pdoc pdoc kawarabanya -o docs/api   # API ドキュメント（docstring から HTML）
```

[Claude Code](https://claude.com/claude-code) を使うなら、`.claude/skills/` のスキル（`kawarabanya-install`・`kawarabanya-discord-setup`・`kawarabanya-ops`）が、インストール・Discord の準備・日々の操作を案内する（「インストールして」「サイトを追加して」などと頼む）。

コードの構成と依存の向き（`cli`・`bot` → `rss` → `core`）、書き方は [docs/CODING.md](docs/CODING.md)。Discord から頼めることを足すときは `src/kawarabanya/bot/assistant.py` の `COMMANDS` に、サイト別の取り込みは `extensions/` に足す。

## 変更の記録

[CHANGELOG.md](CHANGELOG.md)。リリースは [GitHub の Releases](https://github.com/cha2maru/kawarabanya/releases)。

## ライセンス

[MIT](LICENSE)
