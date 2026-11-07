"""OPML の読み込み・書き出し。"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from kawarabanya.core import db


@dataclass
class OpmlEntry:
    """OPML の 1 フィード。"""
    url: str
    title: str | None
    site_url: str | None
    category: str | None


@dataclass
class ParseResult:
    """parse_opml の結果。"""
    entries: list[OpmlEntry] = field(default_factory=list)
    # 同じ URL が複数のフォルダにあったもの（url, 採用しなかったカテゴリ）
    duplicates: list[tuple[str, str | None]] = field(default_factory=list)


def parse_opml(path: str | Path) -> ParseResult:
    """OPML を読み、フィードの一覧を返す。

    ネストしたフォルダは "a/b" 形式のカテゴリになる。
    同じ URL が複数回出てきた場合は最初のものを採用する。
    """
    root = ET.parse(path).getroot()
    body = root.find("body")
    if body is None:
        raise ValueError(f"{path}: <body> がありません")

    result = ParseResult()
    seen: set[str] = set()

    def walk(node: ET.Element, folders: list[str]) -> None:
        for child in node.findall("outline"):
            url = (child.get("xmlUrl") or "").strip()
            label = child.get("title") or child.get("text")
            if url:
                category = "/".join(folders) or None
                if url in seen:
                    result.duplicates.append((url, category))
                else:
                    seen.add(url)
                    result.entries.append(
                        OpmlEntry(
                            url=url,
                            title=label,
                            site_url=child.get("htmlUrl") or None,
                            category=category,
                        )
                    )
            else:
                walk(child, [*folders, label or ""])

    walk(body, [])
    return result


@dataclass
class ImportStats:
    """import_entries で登録・更新・スキップした件数。"""
    added: int = 0
    updated: int = 0
    skipped: int = 0
    new: list[OpmlEntry] = field(default_factory=list)  # 新しく登録したもの


def import_entries(
    conn: sqlite3.Connection, entries: list[OpmlEntry], *, update: bool = False
) -> ImportStats:
    """フィードを DB に登録する。既存の URL は skip（update=True ならタイトル・カテゴリを更新）。"""
    stats = ImportStats()
    with conn:
        for e in entries:
            existing = db.find_feed_by_url(conn, e.url)
            if existing is None:
                db.insert_feed(conn, e.url, e.title, e.site_url, e.category)
                stats.added += 1
                stats.new.append(e)
            elif update:
                conn.execute(
                    "UPDATE feeds SET title = ?, site_url = ?, category = ? WHERE id = ?",
                    (e.title, e.site_url, e.category, existing.id),
                )
                stats.updated += 1
            else:
                stats.skipped += 1
    return stats


def export_opml(feeds: list[db.Feed], path: str | Path, title: str = "kawarabanya subscriptions") -> None:
    """フィードをカテゴリ（"a/b" はネスト）ごとにまとめて OPML に書き出す。"""
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = title
    body = ET.SubElement(root, "body")

    folders: dict[tuple[str, ...], ET.Element] = {(): body}

    def folder(parts: tuple[str, ...]) -> ET.Element:
        if parts not in folders:
            parent = folder(parts[:-1])
            folders[parts] = ET.SubElement(parent, "outline", text=parts[-1], title=parts[-1])
        return folders[parts]

    by_category: dict[tuple[str, ...], list[db.Feed]] = defaultdict(list)
    for f in feeds:
        by_category[tuple(f.category.split("/")) if f.category else ()].append(f)

    for parts, items in by_category.items():
        parent = folder(parts)
        for f in items:
            attrs = {"type": "rss", "text": f.title or f.url, "title": f.title or f.url, "xmlUrl": f.url}
            if f.site_url:
                attrs["htmlUrl"] = f.site_url
            ET.SubElement(parent, "outline", attrs)

    ET.indent(root, "    ")
    ET.ElementTree(root).write(path, encoding="UTF-8", xml_declaration=True)
