"""kawarabanya: 個人用 Discord サーバーを管理する Bot。

今ある機能は RSS（`kawarabanya.rss`）: 記事を全文 Markdown で保存し、ローカル LLM で要約して、
ジャンルごとの Discord フォーラムに投稿する。コマンドは `kawarabanya.cli`、常駐 Bot は `kawarabanya.bot`。
"""

import sys


def main() -> None:
    """コンソールスクリプト `kawarabanya` の入口。CLI を実行し、その終了コードで終わる。"""
    from .cli import main as cli_main

    sys.exit(cli_main())
