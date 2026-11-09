"""REPL 循环与交互指令：读一行 → 是指令就处理，是话就交给会话引擎。

这里没有任何业务决策：窗口怎么裁、怎么重试、token 怎么算，都在 :mod:`strata.chat`。
退出码语义也归这一层（进程出口就是 REPL 循环）。
"""

import argparse
import sys
from typing import Literal

from strata.chat import ChatSession, ChatSettings, TranscriptLog
from strata.cli._terminal import (
    Palette,
    print_error,
    render_banner,
    render_help,
    render_session,
    render_status,
)
from strata.errors import CommandError, StrataError

EXIT_OK = 0
EXIT_FAILURE = 1

_PROMPT = "你 ▸ "

type Outcome = Literal["continue", "exit"]


def run_chat(
    args: argparse.Namespace,
    settings: ChatSettings,
    session: ChatSession,
    palette: Palette,
    transcript: TranscriptLog | None = None,
) -> int:
    """交互式主循环。``--prompt`` 给了就走一次性问答，不进循环。"""
    print(render_banner(session, settings, palette, transcript))
    if args.prompt is not None:
        session.ask(args.prompt)
        return EXIT_OK

    while True:
        try:
            raw = input(_PROMPT)
        except EOFError:
            print()
            return EXIT_OK
        except KeyboardInterrupt:
            print()
            continue
        text = raw.strip()
        if not text:
            continue
        try:
            if text.startswith("/"):
                if _handle_command(text, session, palette, transcript) == "exit":
                    return EXIT_OK
            else:
                session.ask(text)
        except StrataError as exc:
            print_error(exc, palette)
        except OSError as exc:  # 终端 I/O 故障：管道被关、控制台不可用
            sys.stderr.write(palette.red(f"✗ {type(exc).__name__}: {exc}\n"))
            return EXIT_FAILURE


def _parse_toggle(rest: str, *, current: bool, command: str) -> bool:
    match rest.strip().lower():
        case "on" | "show":
            return True
        case "off" | "hide":
            return False
        case "":
            return not current
        case other:
            raise CommandError(f"/{command} 只接受 on 或 off（收到 {other}）", command=command)


def _handle_command(
    text: str, session: ChatSession, palette: Palette, transcript: TranscriptLog | None
) -> Outcome:
    head, _, rest = text[1:].partition(" ")
    match head.lower():
        case "exit" | "quit" | "q" | "bye":
            return "exit"
        case "help" | "h" | "?":
            print(render_help(palette))
        case "history" | "hist":
            print(session.conversation.render(include_thinking=session.show_thinking))
        case "thinking" | "think":
            session.show_thinking = _parse_toggle(
                rest,
                current=session.show_thinking,
                command="thinking",
            )
            state = "显示" if session.show_thinking else "隐藏"
            print(palette.dim(f"🧠 Thinking 已{state}"))
        case "retry" | "r":
            session.retry()
        case "clear" | "reset":
            removed = session.conversation.clear()
            print(palette.dim(f"已清空 {removed} 轮历史"))
        case "status":
            print(render_status(session, transcript))
        case "session":
            print(f"转录 {render_session(transcript)}")
        case _:
            raise CommandError(f"未知指令 /{head}", command=head)
    return "continue"
