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


def test_doctor_reports_missing_setup(tmp_path, monkeypatch, capsys):
    import argparse

    from kawarabanya.cli.doctor import cmd_doctor
    from kawarabanya.core.config import GeneralConfig, SummarizeConfig

    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)  # systemd のサービスは調べない
    cfg = Config(general=GeneralConfig(db=str(tmp_path / "k.db"), archive_dir=str(tmp_path / "archive"),
                                       genres_file=str(tmp_path / "genres.toml")),
                 summarize=SummarizeConfig(endpoint="http://127.0.0.1:9/v1"))
    assert cmd_doctor(cfg, argparse.Namespace(db=None)) == 1
    out = capsys.readouterr().out
    assert "❌ genres.toml" in out and "❌ Discord: Bot のトークンがありません" in out
    assert "✅ DB" in out and "⚠️  要約サーバー" in out
