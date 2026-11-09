"""进程入口的装配层：把参数、配置和引擎零件接起来。

拆得很薄，方便按职责改：参数与环境变量在 :mod:`strata.cli._parser`，终端能力与文案在
:mod:`strata.cli._terminal`，主循环与交互指令在 :mod:`strata.cli._repl`。这个文件只留
装配（``assemble``）与入口（``main``），并稳定再导出公共符号，保证
``strata.cli:main``（控制台脚本）、``python -m strata`` 与测试的导入面不变。
"""

import contextlib
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

from strata import __version__
from strata.chat import (
    ChatSession,
    ChatSettings,
    Conversation,
    TranscriptLog,
    build_backend,
    validate_settings,
)
from strata.cli._parser import build_parser, settings_from_args
from strata.cli._repl import EXIT_FAILURE, EXIT_OK, run_chat
from strata.cli._terminal import (
    Palette,
    PalettePainter,
    make_painter,
    print_error,
    render_banner,
    render_help,
    render_status,
    resolve_palette,
    stdout_pair,
)
from strata.errors import ConfigurationError, StrataError

EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

__all__ = [
    "EXIT_CONFIG",
    "EXIT_FAILURE",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "Palette",
    "PalettePainter",
    "assemble",
    "build_parser",
    "main",
    "make_painter",
    "print_error",
    "render_banner",
    "render_help",
    "render_status",
    "resolve_palette",
    "run_chat",
    "settings_from_args",
    "stdout_pair",
]


def assemble(settings: ChatSettings, palette: Palette) -> tuple[ChatSession, TranscriptLog | None]:
    """装配一次会话；配了 ``--session`` 就顺带恢复历史并挂上落盘 sink。"""
    write, flush = stdout_pair()
    transcript = TranscriptLog(Path(settings.session_file)) if settings.session_file else None
    conversation = Conversation(
        settings.system_prompt,
        max_turns=settings.max_turns,
        max_context_chars=settings.max_context_chars,
    )
    if transcript is not None:
        conversation.load(transcript.turns)
    session = ChatSession(
        backend=build_backend(settings),
        conversation=conversation,
        painter=make_painter(palette),
        write=write,
        flush=flush,
        streaming=settings.streaming,
        show_thinking=settings.show_thinking,
        on_turn=transcript.append if transcript is not None else None,
    )
    if transcript is not None:
        for usage in transcript.usages:  # 上一进程报过的 token 账，接着算
            session.usage.add(usage)
    return session, transcript


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"strata {__version__}")
        print(f"python {platform.python_version()} ({sys.executable})")
        return EXIT_OK
    if args.command != "chat":
        parser.print_help()
        return EXIT_OK

    palette = resolve_palette(no_color=args.no_color)
    try:
        settings = validate_settings(settings_from_args(args))
        session, transcript = assemble(settings, palette)
    except ConfigurationError as exc:
        print_error(exc, palette)
        return EXIT_CONFIG

    try:
        return run_chat(args, settings, session, palette, transcript)
    except KeyboardInterrupt:
        print()
        return EXIT_INTERRUPTED
    except BrokenPipeError:  # 下游管道提前关闭：不算失败
        with contextlib.suppress(OSError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        return EXIT_OK
    except StrataError as exc:  # 会话中途冒出来的引擎异常
        print_error(exc, palette)
        return EXIT_FAILURE
