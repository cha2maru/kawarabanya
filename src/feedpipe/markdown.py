"""記事を Front Matter 付きの Markdown ファイルとして保存する。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from .item import Item

UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')
SPACES = re.compile(r"[\s_]+")
# ext4 のファイル名上限は 255 バイト。日本語（3 バイト）でも収まるよう文字数で切る
MAX_SLUG_CHARS = 50
UNCATEGORIZED = "uncategorized"


def slugify(text: str | None, fallback: str) -> str:
    s = SPACES.sub("_", UNSAFE.sub("_", text or "")).strip("._ ")
    s = s[:MAX_SLUG_CHARS].rstrip("._ ")
    return s or fallback


def _yaml_str(value: str) -> str:
    # JSON の文字列リテラルはそのまま YAML の文字列としても有効
    return json.dumps(value, ensure_ascii=False)


def _strip_title_heading(md: str, title: str | None) -> str:
    """本文の先頭がタイトルと同じ見出しなら取り除く（ファイル側で見出しを付けるため）。"""
    first, _, rest = md.lstrip().partition("\n")
    if title and first.startswith("#"):
        heading = first.lstrip("#").strip()
        if heading and (heading in title or title in heading):
            return rest.lstrip("\n")
    return md


def render(item: Item, fetched: datetime) -> str:
    fm: list[str] = ["---", f"title: {_yaml_str(item.title or '')}"]
    if item.url:
        fm.append(f"url: {_yaml_str(item.url)}")
    fm.append(f"feed: {_yaml_str(item.feed.title or item.feed.url)}")
    if item.feed.category:
        fm.append(f"category: {_yaml_str(item.feed.category)}")
    if item.author:
        fm.append(f"author: {_yaml_str(item.author)}")
    if item.published:
        fm.append(f"published: {item.published.astimezone().isoformat(timespec='seconds')}")
    fm.append(f"fetched: {fetched.astimezone().isoformat(timespec='seconds')}")
    fm.append(f"source: {item.content_source}")
    fm.append("---")

    body = [f"# {item.title or '(無題)'}", ""]
    if item.content_md:
        body += [_strip_title_heading(item.content_md, item.title).strip(), ""]
    if item.url:
        body += ["---", "", f"元記事: <{item.url}>", ""]
    return "\n".join(fm) + "\n\n" + "\n".join(body)


def build_path(archive_dir: Path, item: Item, fetched: datetime) -> Path:
    date = (item.published or fetched).astimezone()
    category = item.feed.category or UNCATEGORIZED
    parts = [slugify(p, UNCATEGORIZED) for p in category.split("/")]
    fallback = hashlib.sha1(item.key.encode()).hexdigest()[:10]
    name = f"{date:%Y-%m-%d}_{slugify(item.title, fallback)}"
    return archive_dir.joinpath(*parts, f"{date:%Y}", f"{date:%m}", name + ".md")


def save(archive_dir: Path, item: Item, fetched: datetime) -> Path:
    """保存して、保存先のパスを返す。同名のファイルがあれば _2, _3 … を付ける。"""
    path = build_path(archive_dir, item, fetched)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate, n = path, 1
    while True:
        try:
            with candidate.open("x", encoding="utf-8") as f:
                f.write(render(item, fetched))
            return candidate
        except FileExistsError:
            n += 1
            candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
