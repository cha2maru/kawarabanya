"""feedpipe: 個人用 Discord サーバーを管理する Bot。

今ある機能は RSS（`feedpipe.rss`）: 記事を全文 Markdown で保存し、ローカル LLM で要約して、
ジャンルごとの Discord フォーラムに投稿する。コマンドは `feedpipe.cli`、常駐 Bot は `feedpipe.bot`。
"""

import sys


def main() -> None:
    """コンソールスクリプト `feedpipe` の入口。CLI を実行し、その終了コードで終わる。"""
    from .cli import main as cli_main

    sys.exit(cli_main())
