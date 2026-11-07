"""URL が RSSHub のどのルートで取れるかを、RSSHub の radar ルールで調べる（RSSHub を動かさなくてよい）。

radar ルールは RSSHub のビルドで作られる JSON（ドメイン → サブドメイン → [{title, docs, source, target}]）。
source は URL のパスのパターン（/category/:category、/*path）、target は RSSHub のルート（/famitsu/category/:category?）。
公式インスタンス（rsshub.app）の API は費用の都合で制限されているので、jsDelivr の配布物を使い、data/ に 7 日置く。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

RULES_URL = "https://cdn.jsdelivr.net/gh/DIYgod/RSSHub@gh-pages/build/radar-rules.json"
CACHE_DAYS = 7


@dataclass
class Route:
    """URL に当たる RSSHub のルート。"""
    title: str
    route: str  # 埋められた引数は埋めたもの。埋まらない引数は :name のまま
    docs: str
    matched: bool  # パスまで当たったか（False ならドメインだけ当たった）


def load_rules(cache: Path, user_agent: str, timeout: float = 30) -> dict:
    """radar ルールを読む（cache が古ければ取り直す。取れなければ古い cache を使う）。"""
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_DAYS * 86400:
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        resp = httpx.get(RULES_URL, headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        rules = resp.json()
    except (httpx.HTTPError, ValueError):
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        raise
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    return rules


def _pattern(source: str) -> re.Pattern:
    """source のパス（:name・*name）を正規表現にする。? と # 以降は見ない。"""
    path = re.split(r"[?#]", source, maxsplit=1)[0].rstrip("/") or "/"
    out, pos = "", 0
    for m in re.finditer(r"([:*])(\w*)", path):
        out += re.escape(path[pos : m.start()])
        name = m.group(2) or f"_{m.start()}"
        out += f"(?P<{name}>[^/]+)" if m.group(1) == ":" else f"(?P<{name}>.*)"
        pos = m.end()
    out += re.escape(path[pos:])
    return re.compile(f"^{out}/?$")


def _fill(target: str, params: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        value = params.get(m.group(1))
        return value if value else ("" if m.group(2) else m.group(0))

    route = re.sub(r":(\w+)(\?)?", sub, target)
    return re.sub(r"/+$", "", re.sub(r"//+", "/", route)) or "/"


def match(url: str, rules: dict) -> list[Route]:
    """url に当たるルート。パスまで当たったものが先、なければドメインだけ当たったルートの一覧。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/") or "/"
    found: list[Route] = []
    for domain, subs in rules.items():
        if host != domain and not host.endswith("." + domain):
            continue
        sub = host[: -len(domain)].rstrip(".") or "."
        for key, rs in subs.items():
            if key == "_name" or (key not in (sub, "*") and not (key == "." and sub == "www")):
                continue
            for r in rs:
                target = r.get("target") or ""
                hits = [m for s in r.get("source", []) if (m := _pattern(s).match(path))]
                params = hits[0].groupdict() if hits else {}
                found.append(Route(r.get("title", ""), _fill(target, params) if hits else target, r.get("docs", ""),
                                   bool(hits)))
    return sorted(found, key=lambda r: not r.matched)
