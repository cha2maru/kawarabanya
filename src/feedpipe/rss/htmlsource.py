"""RSS のないサイトの一覧ページから、CSS セレクタで記事を取り出す（HTML 取り込み）。

設定は genres.toml の feeds に書く:

    { url = "https://example.com/news", item = "article.post", title = "h2", link = "a", date = "time" }

- item（必須）: 記事 1 件の塊。title / link / date はこの中で探す
- title: 省略するとリンクの文字列
- link: 省略すると塊の中（塊が <a> ならそれ自身）の最初の <a> の href
- date: datetime 属性があればそれ、なければ文字列から「2026年11月3日」「2026.11.03」などを読む
- link_pattern: この正規表現に合う URL の記事だけにする（RSS-Bridge の url_pattern と同じ）

取り出した記事は feedparser の entry と同じ形の dict で返すので、以降は RSS と同じ処理を通る。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import lxml.html

# 記事を見分けるキーにする URL から除く、追跡用のパラメータ
TRACKING_PARAMS = re.compile(r"^(utm_\w+|fbclid|gclid|yclid|mc_\w+|ref|ref_src)$")
DATE_RE = re.compile(r"(20\d\d)\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})")
MAX_ITEMS = 50


@dataclass
class HtmlSource:
    item: str
    title: str | None = None
    link: str | None = None
    date: str | None = None
    link_pattern: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> HtmlSource:
        """genres.toml の設定から作る。item がない・知らないキーがあれば ValueError。"""
        known = {f.name for f in fields(cls)}
        if unknown := set(raw) - known:
            raise ValueError(f"HTML 取り込みに不明なキーがあります: {', '.join(sorted(unknown))}")
        if not raw.get("item"):
            raise ValueError("HTML 取り込みには item（記事の塊の CSS セレクタ）が必要です")
        return cls(**raw)


def normalize_url(url: str) -> str:
    """#以降と追跡用パラメータを除く（同じ記事が別物として何度も取り込まれないように）。"""
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _parse(html: bytes | str):
    """HTML を読む。文字コードの指定がないと lxml は latin-1 とみなすので、先に UTF-8 を試す。"""
    if isinstance(html, bytes):
        try:
            html = html.decode("utf-8")
        except UnicodeDecodeError:
            pass  # Shift_JIS など。<meta charset> を見て lxml が読む
    return lxml.html.fromstring(html)


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.text_content()).strip()


def _first(el, selector: str | None):
    if selector is None:
        return None
    found = el.cssselect(selector)
    return found[0] if found else None


def _parse_date(el) -> datetime | None:
    """要素の datetime 属性か文字列から日付を読み、ローカル時刻の日付として返す。"""
    if el is None:
        return None
    if value := el.get("datetime"):
        try:
            dt = datetime.fromisoformat(value)  # 3.11 以降は末尾の Z も読める
            return dt if dt.tzinfo else dt.astimezone()
        except ValueError:
            pass
    if m := DATE_RE.search(_text(el)):
        try:
            return datetime(*map(int, m.groups())).astimezone()
        except ValueError:
            return None
    return None


def extract(html: bytes | str, base_url: str, src: HtmlSource) -> list[dict]:
    """一覧ページから記事を取り出す（ページの上から順、最大 MAX_ITEMS 件、同じ URL は 1 件）。"""
    root = _parse(html)
    pattern = re.compile(src.link_pattern) if src.link_pattern else None
    entries: dict[str, dict] = {}
    for item in root.cssselect(src.item):
        a = _first(item, src.link) if src.link else (item if item.tag == "a" else _first(item, "a[href]"))
        href = a.get("href") if a is not None else None
        if not href:
            continue
        url = normalize_url(urljoin(base_url, href))
        if (pattern and not pattern.search(url)) or url in entries:
            continue
        title_el = _first(item, src.title)
        title = _text(title_el) if title_el is not None else _text(a)
        published = _parse_date(_first(item, src.date))
        entries[url] = {
            "id": url,
            "link": url,
            "title": title,
            # feedparser と同じく UTC の time.struct_time
            "published_parsed": published.utctimetuple() if published else None,
            # 元記事から本文を取れなかったときに使う
            "summary": lxml.html.tostring(item, encoding="unicode"),
        }
        if len(entries) >= MAX_ITEMS:
            break
    return list(entries.values())


def page_title(html: bytes | str) -> str | None:
    """ページの <title>（フィード名の代わりに使う）。"""
    titles = _parse(html).findall(".//title")
    return _text(titles[0]) or None if titles else None
