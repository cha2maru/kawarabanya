"""設定ファイル（config.toml）の読み込み。ファイルがなければ既定値で動く。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.toml")
DEFAULT_ENV_PATH = Path(".env")


def load_dotenv(path: Path = DEFAULT_ENV_PATH) -> None:
    """KEY=VALUE 形式の .env を読み、未設定の環境変数だけ設定する。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass
class GeneralConfig:
    db: str = "data/feedpipe.db"
    archive_dir: str = "archive"
    concurrency: int = 16
    per_host: int = 2
    timeout: float = 20.0
    user_agent: str = "Mozilla/5.0 (compatible; feedpipe/0.1)"
    # 連続失敗がこの回数に達したフィードは自動で無効化する
    max_failures: int = 5
    # 最新記事がこの日数より古いフィードを「放置」として報告する（無効化はしない）
    stale_days: int = 365


@dataclass
class FulltextConfig:
    enabled: bool = True
    # 元記事から抽出した本文がこの文字数より短ければ、フィードの本文と長い方を使う
    min_length: int = 200
    include_images: bool = True


@dataclass
class DiscordConfig:
    # Bot が参加しているサーバーが 1 つだけなら省略できる
    guild_id: str | None = None
    # feedpipe のチャンネルをまとめる Discord のカテゴリ名
    parent_category: str = "feedpipe"
    webhook_name: str = "feedpipe"
    # フォーラムを年齢制限チャンネルにする
    nsfw: bool = True
    # 1 回の実行で投稿する上限（超えた分は次回に回す）
    max_posts_per_run: int = 30
    token_env: str = "DISCORD_BOT_TOKEN"

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env) or None


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    fulltext: FulltextConfig = field(default_factory=FulltextConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    path: Path | None = None


def _section(cls: type, raw: dict, name: str, path: Path):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{path}: [{name}] に不明なキーがあります: {', '.join(sorted(unknown))}")
    return cls(**raw)


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Config(
        general=_section(GeneralConfig, raw.get("general", {}), "general", path),
        fulltext=_section(FulltextConfig, raw.get("fulltext", {}), "fulltext", path),
        discord=_section(DiscordConfig, raw.get("discord", {}), "discord", path),
        path=path,
    )
