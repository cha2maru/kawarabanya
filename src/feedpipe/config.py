"""設定ファイル（config.toml）の読み込み。ファイルがなければ既定値で動く。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.toml")


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
class Config:
    general: GeneralConfig
    path: Path | None = None


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config(general=GeneralConfig())
    with path.open("rb") as f:
        raw = tomllib.load(f)
    known = {f.name for f in fields(GeneralConfig)}
    general_raw = raw.get("general", {})
    unknown = set(general_raw) - known
    if unknown:
        raise ValueError(f"{path}: [general] に不明なキーがあります: {', '.join(sorted(unknown))}")
    return Config(general=GeneralConfig(**general_raw), path=path)
