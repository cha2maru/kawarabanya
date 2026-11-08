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
    """[general]: DB・保存先・HTTP 取得・フィードの自動無効化。"""
    db: str = "data/kawarabanya.db"
    archive_dir: str = "archive"
    genres_file: str = "genres.toml"
    # サイト別の取り込み（拡張）を置くディレクトリ（rss/sources/extensions.py）
    extensions_dir: str = "extensions"
    # 自分で動かしている RSSHub の URL（例: "http://127.0.0.1:1200"）。doctor が動いているかを確かめる。空なら使わない
    rsshub_url: str = ""
    concurrency: int = 16
    per_host: int = 2
    timeout: float = 20.0
    user_agent: str = "Mozilla/5.0 (compatible; kawarabanya/0.1)"
    # 連続失敗がこの回数に達したフィードは自動で無効化する
    max_failures: int = 5
    # 最新記事がこの日数より古いフィードを「放置」として報告する（無効化はしない）
    stale_days: int = 365


@dataclass
class FulltextConfig:
    """[fulltext]: 元記事からの本文抽出。"""
    enabled: bool = True
    # 元記事から抽出した本文がこの文字数より短ければ、フィードの本文と長い方を使う
    min_length: int = 200
    include_images: bool = True


@dataclass
class SummarizeConfig:
    """[summarize]: 要約サーバー（OpenAI 互換 API）と、重要度の判定・要約の条件。"""
    enabled: bool = True
    endpoint: str = "http://127.0.0.1:8080/v1"
    model: str = "Qwen3.5-4B"
    # 読み込みが CPU 時間の大半なので短めにする（3,000 → 1,000 字で 16 秒 → 9 秒、品質はほぼ同じ）
    max_input_chars: int = 1000
    max_tokens: int = 300
    temperature: float = 0.3
    timeout: float = 180.0
    # 公開からこの日数以内の記事だけ要約する（通知しない古い記事に CPU を使わない）
    max_age_days: int = 3
    # 要約の前に重要度（A の確率）を判定し、これ以上の記事だけ要約する。0 なら判定せず全件要約する
    priority_threshold: float = 0.8
    # 見出しの似た直近の記事と同じ出来事かを判定し、確率がこれ以上なら後の方を投稿しない。0 なら判定しない
    same_story_threshold: float = 0.9


@dataclass
class DiscordConfig:
    # Bot が参加しているサーバーが 1 つだけなら省略できる
    """[discord]: Bot・フォーラム・投稿の設定。トークンは環境変数（.env）から読む。"""
    guild_id: str | None = None
    # kawarabanya のチャンネルをまとめる Discord のカテゴリ名
    parent_category: str = "kawarabanya"
    # 同じ名前の Webhook で投稿したメッセージしか書き換え・削除できないので、運用中に変えない
    webhook_name: str = "kawarabanya"
    # フォーラムを年齢制限チャンネルにする
    nsfw: bool = True
    # 1 回の実行で投稿する上限（超えた分は次回に回す）
    max_posts_per_run: int = 30
    # 公開からこの日数を過ぎた記事は投稿しない（未読でも stale にする）
    max_age_days: int = 3
    # まとめ・予定でメンションする自分のユーザー ID（Discord の開発者モードで「ユーザー ID をコピー」）。空ならしない
    mention_user_id: str = ""
    token_env: str = "DISCORD_BOT_TOKEN"

    @property
    def token(self) -> str | None:
        """Bot トークン（token_env の環境変数）。未設定なら None。"""
        return os.environ.get(self.token_env) or None


@dataclass
class DigestConfig:
    """[digest]: まとめの投稿先・時刻・件数。"""
    enabled: bool = True
    # まとめを投稿するテキストチャンネルの名前（サーバー内で探す。カテゴリは問わない）
    channel: str = "ニュース"
    # まとめを投稿する時刻（ローカル時刻 "HH:MM"）。前回のまとめ以降の記事をまとめる
    times: list[str] = field(default_factory=lambda: ["06:30", "18:30"])
    # 前回のまとめがない・古いときに、さかのぼる時間数の上限と、リンクを並べる件数
    window_hours: int = 24
    top: int = 10
    # Google トレンドの急上昇ワードの地域（話題になった記事をまとめの上に寄せる）。空なら使わない
    trends_geo: str = "JP"


@dataclass
class ScheduleConfig:
    """[schedule]: Google カレンダーの予定の通知（gcsa・読み取り専用の OAuth）。"""
    enabled: bool = False
    # Google Cloud の OAuth クライアント ID（デスクトップ アプリ）の JSON と、許可して得たトークンの置き場所
    credentials: str = "data/google-credentials.json"
    token: str = "data/google-token.json"
    # 予定を投稿するテキストチャンネル（持ち主と Bot だけが見られるようにしておく）
    channel: str = "予定"
    # 読むカレンダーの ID。空ならすべて
    calendars: list[str] = field(default_factory=list)
    # 開始の何分前に知らせるか・何日先まで読むか・今日と明日の予定を投稿する時刻
    remind_minutes: int = 30
    days: int = 14
    agenda_times: list[str] = field(default_factory=lambda: ["06:30"])
    # 許可のときにブラウザから戻ってくるポート（既定の 8080 は要約サーバーと重なる）
    login_port: int = 8765


@dataclass
class BotConfig:
    # 常駐中に `kawarabanya run` を実行する間隔（分）。0 なら実行しない
    """[bot]: 常駐 Bot の定期実行と、一緒に起動する要約サーバー。"""
    run_interval_minutes: int = 60
    # run に追加で渡す引数（例: ["--no-summary"]）
    run_args: list[str] = field(default_factory=list)
    # Bot と一緒に起動・停止する要約サーバーのコマンド（例: "scripts/llama-server.sh"）。空なら起動しない
    llm_server_command: str = ""


@dataclass
class Config:
    """config.toml 全体。省略したセクション・キーは既定値になる。"""
    general: GeneralConfig = field(default_factory=GeneralConfig)
    fulltext: FulltextConfig = field(default_factory=FulltextConfig)
    summarize: SummarizeConfig = field(default_factory=SummarizeConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    path: Path | None = None


def _section(cls: type, raw: dict, name: str, path: Path):
    """TOML の 1 セクションを設定クラスにする。知らないキーがあれば ValueError（書き間違いに気づけるように）。"""
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{path}: [{name}] に不明なキーがあります: {', '.join(sorted(unknown))}")
    return cls(**raw)


def load_config(path: Path | None = None) -> Config:
    """config.toml を読む。ファイルがなければすべて既定値。"""
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Config(
        general=_section(GeneralConfig, raw.get("general", {}), "general", path),
        fulltext=_section(FulltextConfig, raw.get("fulltext", {}), "fulltext", path),
        summarize=_section(SummarizeConfig, raw.get("summarize", {}), "summarize", path),
        discord=_section(DiscordConfig, raw.get("discord", {}), "discord", path),
        bot=_section(BotConfig, raw.get("bot", {}), "bot", path),
        digest=_section(DigestConfig, raw.get("digest", {}), "digest", path),
        schedule=_section(ScheduleConfig, raw.get("schedule", {}), "schedule", path),
        path=path,
    )
