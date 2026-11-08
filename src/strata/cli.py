"""命令行入口：``strata`` 控制台脚本与 ``python -m strata``。"""

import argparse
import platform
import sys
from collections.abc import Sequence

from strata import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strata", description="Strata 工程命令行入口")
    parser.add_argument(
        "--version",
        action="store_true",
        help="打印包版本与当前 Python 运行时信息",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"strata {__version__}")
        print(f"python {platform.python_version()} ({sys.executable})")
        return 0
    build_parser().print_help()
    return 0
