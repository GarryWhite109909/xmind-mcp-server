"""命令行入口：直接启动 MCP Server。"""

import sys

from .cli import main


if __name__ == "__main__":
    sys.exit(main())
