"""feedpipe: RSS を全文 Markdown で保存し、Discord に通知するパイプライン。"""

import sys


def main() -> None:
    from .cli import main as cli_main

    sys.exit(cli_main())
