# kawarabanya（瓦版屋）

個人用 Discord サーバーを管理する Bot。今ある機能は RSS: 記事を全文 Markdown で保存し、ローカル LLM で要約して、ジャンルごとの Discord フォーラムに投稿する。設計は [DESIGN.md](DESIGN.md)。

旧名は feedpipe（同名のプロジェクトがあったため 2026-11-07 に改名）。そのころ作った Discord のカテゴリと Webhook は、`config.toml` の `[discord] parent_category` / `webhook_name` を `"feedpipe"` にして使い続ける。

## セットアップ

```sh
uv sync
cp config.example.toml config.toml           # 設定（すべて省略可。変えたいところだけ残す）

# 1. Discord Bot（https://discord.com/developers/applications で作成）
cp .env.example .env                         # DISCORD_BOT_TOKEN を書く
uv run kawarabanya discord invite-url           # この URL からサーバーに招待する（サーバーは「コミュニティ」を有効に）

# 2. フィードとジャンル
uv run kawarabanya import-opml feedly.opml
uv run kawarabanya check --disable-dead         # つながらないフィードを無効化
cp genres.example.toml genres.toml           # ジャンルとフィードの URL を書く
uv run kawarabanya genres apply --disable-unassigned

# 3. Discord のフォーラム（ジャンルごと）を作る
uv run kawarabanya discord setup                # 予定を表示
uv run kawarabanya discord setup --apply

# 4. 要約用のモデル（llama.cpp と Qwen3.5-4B）を用意し、scripts/llama-server.sh のパスを合わせる

# 5. 常駐 Bot をサービスとして起動
cp scripts/kawarabanya-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now kawarabanya-bot
journalctl --user -u kawarabanya-bot -f         # ログ
```

### RSS のないサイト（RSSHub・任意）

[RSSHub](https://docs.rsshub.app/) を Docker で動かすと、RSS のないサイトも普通のフィードとして登録できる（kawarabanya 側の変更は不要）。

```sh
docker run -d --name rsshub --restart unless-stopped -p 127.0.0.1:1200:1200 diygod/rsshub
uv run kawarabanya feeds add "http://127.0.0.1:1200/github/repos/DIYgod" --genre tech --no-discover   # genres.toml にも書く
```

ルートの一覧は `curl http://127.0.0.1:1200/api/namespace`。ルートによっては API キーなどの設定（環境変数）が要る。

## コマンド

| コマンド | 内容 |
|---|---|
| `bot` | 常駐 Bot。Bot にメンションして日本語で頼むと、コマンドに直して返事をする（下の「Discord から頼む」）。60 分ごとと、まとめの時刻（6:30・18:30）に `run`・`genres suggest --apply --due`・`digest --due` を順に別プロセスで実行する |
| `digest [--genre X] [--due] [--dry-run]` | ジャンルごとのまとめ（前回のまとめ以降の記事の傾向＋重要度順の投稿へのリンク）を今すぐ #ニュース に投稿する。Bot は 6:30・18:30 に投稿する |
| `digest --update [--genre X]` | 最後に投稿したまとめを、最新の記事・重要度で作り直して書き換える |
| `digest --delete [--genre X] [--day YYYY-MM-DD]` | ジャンルごとに最後のまとめ（`--day` ならその日のすべて）を削除する |
| `run [--category X] [--backfill N] [--no-fulltext] [--no-summary] [--no-notify]` | 取得 → 保存 → 重要度の判定 → 要約 → 投稿 → 👍 集計。初回のフィードは既存記事を既読にするだけ |
| `summarize` | 保存済み・未投稿で要約のない最近の記事を、重要度を判定してから要約する |
| `notify [--category X] [--limit N] [--dry-run]` | 保存済み・未投稿の記事を投稿する |
| `likes` / `interests [--days 30]` | 👍 の数を取得する / ジャンル・フィード別の 👍 の割合と、最近 👍 した記事（投稿へのリンク付き）を表示する |
| `import-opml FILE` / `export-opml FILE [--all]` | OPML の取り込み・書き出し |
| `check [--disable-dead]` | 死活チェック（移転していれば URL を更新） |
| `feeds add URL [--genre G] [--title T] [--no-discover]` | サイトかフィードの URL から登録する。ページ内の案内（`<link rel="alternate">`）やよくある場所（/feed など）から RSS を探し、genres.toml の `G` にも書き足す。初回は既存の記事を既読にするだけ |
| `feeds rsshub URL` | URL が RSSHub のどのルートで取れるかを調べる（RSSHub の radar ルールを jsDelivr から取って `data/` に 7 日置く。RSSHub は動かさなくてよい）。`feeds add` でフィードが見つからないときも案内する |
| `feeds remove ID\|URL` | 無効にして genres.toml から外す（保存済みの記事と投稿は残す） |
| `feeds list\|enable\|disable\|option` | フィードの管理（例: `feeds option ID no_fulltext true`） |
| `genres interest G like\|normal\|low\|mute` | ジャンル・サブジャンルの好き嫌いを genres.toml に書く |
| `status` | 最後の取り込み・次のまとめ・要約サーバー・失敗中のフィード・記事の件数 |
| `genres list` / `genres apply [--dry-run] [--disable-unassigned]` | ジャンルの一覧 / genres.toml の反映 |
| `genres suggest [--apply] [--days 7] [--due]` | どのサブジャンルにも当てはまらなかった記事から、新しいサブジャンルを LLM に決めさせる（案は振り分け直して確かめる）。タグの上限（20）を超えるジャンルは似たサブジャンルを先にまとめる。`--apply` で genres.toml に反映し、Discord のタグを揃えて付け直す（Bot はまとめの時刻ごとに自動で実行する） |
| `discord setup [--apply] [--prune]` / `channels` / `test GENRE` / `guilds` / `invite-url` | フォーラム・タグ・Webhook の管理 |
| `discord retag` | 投稿済みの記事のタグをサブジャンルに付け直す（未判定なら LLM で判定。Bot に「スレッドの管理」が必要） |
| `discord delete-duplicates [--apply]` | 重複（同じ URL・同じ本文）と判定した記事のフォーラムの投稿を消す（なしは一覧だけ。「スレッドの管理」が必要） |

全体のオプション: `-c config.toml`、`--db PATH`、`-v`（詳しいログ）。

## 設定

### Discord から頼む

Bot にメンションして日本語で書くと、ローカル LLM がコマンドに直す（サーバーの持ち主だけ）。Developer Portal の Bot の設定で **MESSAGE CONTENT INTENT** をオンにしておく（「@RSSBOT」は Bot のロールへのメンションになることがあり、そのときは本文を読む許可が要る）。サイトの追加・削除、好き嫌いの変更、取り込み・まとめの実行は、ボタンで確認してから実行する。状態や一覧はすぐ返す。

```
@RSSBOT https://example.com/news をボードゲームに追加して   → feeds add … --genre hobby/boardgame（確認）
@RSSBOT GIGAZINE を外して                                    → feeds remove 182（確認）
@RSSBOT スポーツはまとめに出さなくていい                     → genres interest culture/sports low（確認）
@RSSBOT いまどうなってる？                                   → status
@RSSBOT 何ができるの？                                       → 例の一覧
```

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

[hobby.boardgame]
label = "ボードゲーム"
feeds = [ ... ]              # サブジャンルの下に書いたフィードは、判定せずにこのサブジャンルになる

[culture.sports]
label = "スポーツ"
interest = "low"             # 好き嫌い: like（まとめの上位に寄せる）/ low（投稿はするが、まとめに載せない）/ mute（投稿もしない）
                             # 書かなければ普通。サブジャンルに書かなければジャンルの値を受け継ぐ（digest = false は low の古い書き方）
```

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

## 開発

```sh
uv run pytest
uvx ruff check src tests
uv run --with pdoc pdoc kawarabanya -o docs/api   # API ドキュメント（docstring から HTML。docs/api/index.html を開く）
```

対話機能（スラッシュコマンドなど）は `src/kawarabanya/bot.py` の `Bot` に足していく。
