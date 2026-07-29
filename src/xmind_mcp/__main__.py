"""命令行入口：首次启动检测硬件、安装依赖；之后直接启动 MCP Server。"""

import sys

from .cli import main


if __name__ == "__main__":
    sys.exit(main())
