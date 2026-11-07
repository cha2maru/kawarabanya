"""サイト別の取り込み（拡張）。RSS のないサイトの一覧ページや API から記事を取り出す。

本体がページを取得し（条件付き GET・リダイレクト・失敗の数え方は RSS と同じ）、拡張は中身から記事を取り出すだけ。
拡張は extensions/<名前>.py に書く（[general] extensions_dir）。genres.toml の feeds に

    { url = "https://example.com/news", extension = "<名前>", name = "表示名", ...拡張の設定 }

と書くと使う。extension を省くと組み込みの html（CSS セレクタ。rss/htmlsource.py）。

拡張のファイルに書くもの:

    def parse(content: bytes, url: str, options: dict) -> list[dict]:
        # 記事を feedparser の entry と同じ形の dict で返す:
        # {"id": ..., "link": ..., "title": ..., "published_parsed": time.struct_time | None, "summary": HTML}
        ...

    def validate(options: dict) -> None:   # 省略可。設定が正しくなければ ValueError
    def title(content: bytes) -> str | None:   # 省略可。フィードの名前（省略時はページの <title>）

何度も取得が要るサイト（目次から子のページをたどる・記事ごとに API を呼ぶ）は、parse の代わりに

    async def fetch(fetcher, url: str, options: dict) -> list[dict]:   # fetcher.get(url) で取得する

を書く（条件付き GET はしない）。組み込みの sitemap（rss/sitemap.py）がこの形。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from . import sitemap
from .htmlsource import HtmlSource, extract, page_title

# 拡張を置くディレクトリ。CLI が [general] extensions_dir で上書きする
DIR = Path("extensions")
BUILTIN = "html"
# genres.toml の feeds のテーブルのうち、拡張の設定ではないキー
ENTRY_KEYS = {"url", "name", "extension"}


@dataclass
class Extension:
    """サイト別の取り込み 1 つ。"""
    name: str
    parse: Callable[[bytes, str, dict], list[dict]] | None = None
    validate: Callable[[dict], None] | None = None
    title: Callable[[bytes], str | None] = page_title
    fetch: Callable[..., Awaitable[list[dict]]] | None = None  # 自分で取得する拡張


def _html_parse(content: bytes, url: str, options: dict) -> list[dict]:
    return extract(content, url, HtmlSource.from_dict(options))


_loaded: dict[str, Extension] = {
    BUILTIN: Extension(BUILTIN, _html_parse, HtmlSource.from_dict),
    "sitemap": Extension("sitemap", validate=sitemap.validate, fetch=sitemap.fetch),
}


def get(name: str) -> Extension:
    """名前の拡張（組み込みか DIR/<名前>.py）。なければ ValueError。"""
    if name in _loaded:
        return _loaded[name]
    path = DIR / f"{name}.py"
    if not name.isidentifier() or not path.exists():
        raise ValueError(f"拡張 {name} がありません（{path} を作る）")
    spec = importlib.util.spec_from_file_location(f"kawarabanya_ext_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parse, fetch = getattr(module, "parse", None), getattr(module, "fetch", None)
    if not callable(parse) and not callable(fetch):
        raise ValueError(f"{path} に parse(content, url, options) か fetch(fetcher, url, options) がありません")  # noqa: TRY004
    ext = Extension(name, parse, getattr(module, "validate", None), getattr(module, "title", page_title), fetch)
    _loaded[name] = ext
    return ext


def split_entry(entry: dict) -> tuple[str, dict]:
    """genres.toml の feeds のテーブルを (拡張の名前, 拡張の設定) にし、設定を確かめる。正しくなければ ValueError。"""
    name = entry.get("extension", BUILTIN)
    options = {k: v for k, v in entry.items() if k not in ENTRY_KEYS}
    ext = get(name)
    if ext.validate:
        ext.validate(options)
    return name, options


def available() -> list[str]:
    """使える拡張の名前（組み込み＋DIR にあるもの）。"""
    files = sorted(p.stem for p in DIR.glob("*.py") if p.stem.isidentifier()) if DIR.exists() else []
    return [*(n for n in _loaded if n in (BUILTIN, "sitemap")), *files]
