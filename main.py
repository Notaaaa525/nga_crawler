"""NGA 特化爬虫唯一入口：只负责把退出码交给进程。"""
from __future__ import annotations

import sys

import cli


if __name__ == "__main__":
    sys.exit(cli.main())
