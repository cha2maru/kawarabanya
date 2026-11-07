#!/usr/bin/env bash
# kawarabanya のインストール。リポジトリのどこから実行してもよい。
#
#   scripts/install.sh            依存を入れ、設定の見本をコピーし（あるものは上書きしない）、環境を調べる
#   scripts/install.sh --llm      要約用の llama.cpp（CPU 版）と Qwen3.5-4B（約 2.7 GB）を ~/.local に入れる
#   scripts/install.sh --service  systemd のユーザーサービス kawarabanya-bot を登録して起動する
#
# 何度実行してもよい（入っているものは飛ばす）。
set -euo pipefail
cd "$(dirname "$0")/.."

LLAMA_VERSION=${LLAMA_VERSION:-b12182}
LLAMA_DIR=${LLAMA_DIR:-$HOME/.local/opt/llama.cpp-$LLAMA_VERSION/llama-$LLAMA_VERSION}
LLAMA_URL="https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_VERSION/llama-$LLAMA_VERSION-bin-ubuntu-x64.tar.gz"
MODEL=${LLAMA_MODEL:-$HOME/.local/share/models/Qwen3.5-4B-Q4_K_M.gguf}
MODEL_URL="https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q4_K_M.gguf"

with_llm=0 with_service=0
for arg in "$@"; do
  case "$arg" in
    --llm) with_llm=1 ;;
    --service) with_service=1 ;;
    -h|--help) sed -n 2,8p "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "不明なオプション: $arg（--llm / --service）" >&2; exit 2 ;;
  esac
done

step() { printf '\n== %s\n' "$1"; }

step "uv と依存パッケージ"
if ! command -v uv >/dev/null; then
  echo "uv がありません。先に入れてください: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi
uv sync

step "設定ファイル（見本からコピー。あるものはそのまま）"
for pair in "config.example.toml:config.toml" "genres.example.toml:genres.toml" ".env.example:.env"; do
  src=${pair%%:*} dst=${pair##*:}
  if [ -e "$dst" ]; then echo "  $dst はあります"; else cp "$src" "$dst"; echo "  $dst を作りました（$src から）"; fi
done
chmod 600 .env

if [ "$with_llm" = 1 ]; then
  step "要約用の llama.cpp（$LLAMA_VERSION・CPU 版）"
  if [ -x "$LLAMA_DIR/llama-server" ]; then
    echo "  $LLAMA_DIR にあります"
  else
    mkdir -p "$(dirname "$LLAMA_DIR")"
    curl -fL --progress-bar "$LLAMA_URL" | tar -xz -C "$(dirname "$LLAMA_DIR")"
    echo "  $LLAMA_DIR に入れました"
  fi
  if ! ldconfig -p 2>/dev/null | grep -q 'libgomp\.so\.1' && [ ! -e "$LLAMA_DIR/libgomp.so.1" ]; then
    # llama.cpp は libgomp（OpenMP）を使う。sudo apt install libgomp1 でもよい
    echo "  libgomp がないので、パッケージから取り出して $LLAMA_DIR に置きます"
    tmp=$(mktemp -d)
    (cd "$tmp" && apt-get download libgomp1 >/dev/null && dpkg-deb -x libgomp1_*.deb x)
    cp "$tmp"/x/usr/lib/x86_64-linux-gnu/libgomp.so.1* "$LLAMA_DIR/"
    rm -rf "$tmp"
  fi
  step "要約モデル（Qwen3.5-4B Q4_K_M）"
  if [ -s "$MODEL" ]; then
    echo "  $MODEL にあります"
  else
    mkdir -p "$(dirname "$MODEL")"
    curl -fL -C - --progress-bar -o "$MODEL.part" "$MODEL_URL" && mv "$MODEL.part" "$MODEL"
    echo "  $MODEL に入れました"
  fi
  # Bot と一緒に要約サーバーを起動・停止する設定にする（自分で書き換えていればそのまま）
  if grep -q '^llm_server_command = ""' config.toml; then
    sed -i 's#^llm_server_command = ""#llm_server_command = "scripts/llama-server.sh"#' config.toml
    echo "  config.toml の [bot] llm_server_command を scripts/llama-server.sh にしました"
  fi
fi

if [ "$with_service" = 1 ]; then
  step "systemd のユーザーサービス kawarabanya-bot"
  unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  sed "s#%h/workspace/rss#$PWD#g" scripts/kawarabanya-bot.service > "$unit_dir/kawarabanya-bot.service"
  systemctl --user daemon-reload
  systemctl --user enable --now kawarabanya-bot
  systemctl --user --no-pager status kawarabanya-bot | head -3
  echo "  ログ: journalctl --user -u kawarabanya-bot -f"
  echo "  ログインしていない間も動かすには: sudo loginctl enable-linger $USER"
fi

step "環境の確認（kawarabanya doctor）"
uv run kawarabanya doctor || true

cat <<'MSG'

次にすること（README の「セットアップ」）:
  1. .env に Discord Bot のトークンを書き、uv run kawarabanya discord invite-url の URL から Bot を招待する
  2. genres.toml にジャンルとフィードを書き、uv run kawarabanya genres apply
  3. uv run kawarabanya discord setup --apply でフォーラムを作る
  4. uv run kawarabanya run で取り込みを試し、scripts/install.sh --service で常駐させる
MSG
