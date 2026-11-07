from dataclasses import fields
from pathlib import Path

from kawarabanya.core.config import Config, load_config

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


def test_config_example_matches_defaults():
    cfg = load_config(EXAMPLE)
    cfg.path = None
    assert cfg == Config()  # 見本の値は既定値
    text = EXAMPLE.read_text(encoding="utf-8")
    for section in fields(Config):
        if section.name == "path":
            continue
        assert f"[{section.name}]" in text
        for f in fields(getattr(Config(), section.name)):
            assert f"\n{f.name} =" in text or f"\n# {f.name} =" in text, f"[{section.name}] {f.name} が見本にない"
