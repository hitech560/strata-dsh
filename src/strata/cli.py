"""命令行入口：``strata`` 控制台脚本与 ``python -m strata``。

这一层只做四件事——参数解析、环境变量覆盖、依赖装配、终端 I/O（见
``docs/architecture.md``）：值对象与端口在 :mod:`strata.core`，会话引擎与提供方客户端在
:mod:`strata.chat`，业务规则不在这里。

用法：

* ``strata chat``                  进入交互式聊天（默认 Strata 本地网关，流式 + 🧠 思维链）
* ``strata chat --prompt "X"``     非交互，只发一条就退出
* ``strata chat --backend local``  断网兜底，整条链路照跑
"""

import argparse
import contextlib
import os
import platform
import sys
from collections.abc import Callable, Sequence
from typing import Literal

from strata import __version__
from strata.chat import (
    BACKEND_CHOICES,
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_CONTEXT_CHARS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    ChatSession,
    ChatSettings,
    Conversation,
    build_backend,
    validate_settings,
)
from strata.core import Lane, LanePainter
from strata.errors import (
    CommandError,
    ConfigurationError,
    ContextOverflowError,
    NothingToRetryError,
    ProviderAuthError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnreachableError,
    StrataError,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

_PROMPT = "你 ▸ "

type Outcome = Literal["continue", "exit"]


# --------------------------------------------------------------------------- 终端样式


class Palette:
    """ANSI 样式。``enabled=False`` 时原样返回，保证管道与重定向拿到干净文本。"""

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\x1b[{code}m{text}\x1b[0m"

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)


def resolve_palette(*, no_color: bool) -> Palette:
    """显式开关与环境变量决定要不要上色；非 TTY 一律不上色。"""
    if no_color:
        return Palette(enabled=False)
    if os.environ.get("FORCE_COLOR"):
        return Palette(enabled=True)
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return Palette(enabled=False)
    return Palette(enabled=sys.stdout.isatty())


class PalettePainter:
    """把引擎给出的语义色道翻译成 ANSI：引擎只说“这是什么”，这里说“长什么样”。"""

    def __init__(self, palette: Palette) -> None:
        self.palette = palette

    def __call__(self, text: str, lane: Lane) -> str:
        match lane:
            case "thinking" | "muted":
                return self.palette.dim(text)
            case "notice":
                return self.palette.cyan(text)
            case "warn":
                return self.palette.yellow(text)
            case "answer":
                return text


def make_painter(palette: Palette) -> LanePainter:
    """装配点：把 :class:`PalettePainter` 作为 :class:`~strata.core.LanePainter` 端口注入。"""
    return PalettePainter(palette)


def stdout_pair() -> tuple[Callable[[str], None], Callable[[], None]]:
    """标准输出的写/刷一对；老终端（GBK 之类）画不出 emoji 时降级，不炸整条流。"""

    def write(text: str) -> None:
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            encoding = sys.stdout.encoding or "ascii"
            sys.stdout.write(text.encode(encoding, "replace").decode(encoding, "replace"))

    def flush() -> None:
        sys.stdout.flush()

    return write, flush


# --------------------------------------------------------------------------- 参数 → 配置


def settings_from_args(args: argparse.Namespace) -> ChatSettings:
    """命令行优先，其次环境变量，最后引擎默认值。校验交给引擎的 ``validate_settings``。"""
    api_key = args.api_key
    if api_key is None:
        api_key = os.environ.get("STRATA_API_KEY", DEFAULT_API_KEY)
    return ChatSettings(
        backend=args.backend,
        base_url=args.base_url or os.environ.get("STRATA_BASE_URL") or DEFAULT_BASE_URL,
        model=args.model or os.environ.get("STRATA_MODEL") or DEFAULT_MODEL,
        api_key=api_key,
        timeout=args.timeout,
        max_retries=args.max_retries,
        max_turns=args.max_turns,
        max_context_chars=args.max_context_chars,
        system_prompt=args.system or os.environ.get("STRATA_SYSTEM_PROMPT", ""),
        show_thinking=args.thinking == "show",
        streaming=not args.no_stream,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )


def assemble(settings: ChatSettings, palette: Palette) -> ChatSession:
    """把引擎零件装配成一次会话：后端 + 上下文记忆 + 色道翻译 + 输出流。"""
    write, flush = stdout_pair()
    return ChatSession(
        backend=build_backend(settings),
        conversation=Conversation(
            settings.system_prompt,
            max_turns=settings.max_turns,
            max_context_chars=settings.max_context_chars,
        ),
        painter=make_painter(palette),
        write=write,
        flush=flush,
        streaming=settings.streaming,
        show_thinking=settings.show_thinking,
    )


# --------------------------------------------------------------------------- 终端文案


def _banner(session: ChatSession, settings: ChatSettings, palette: Palette) -> str:
    thinking = "显示" if session.show_thinking else "隐藏"
    stream = "逐 token" if session.streaming else "整段"
    head = palette.bold(f"Strata 聊天 · 模型 {settings.model} · 后端 {session.backend.name}")
    return "\n".join(
        (
            head + palette.dim(f" · 目标 {session.backend.target}"),
            palette.dim(
                f"上下文 {settings.max_turns} 轮 / {settings.max_context_chars} 字 · "
                f"🧠 Thinking {thinking} · 流式 {stream}"
            ),
            palette.dim("指令 /help · 退出 /exit"),
        )
    )


def _help_text(palette: Palette) -> str:
    rows = (
        ("/help, /h", "看这份指令表"),
        ("/history", "看历史轮次（含每轮思维链摘要）"),
        ("/thinking [on|off]", "切换 🧠 Thinking 是否显示"),
        ("/retry, /r", "重放上一轮输入（改口重问）"),
        ("/clear", "清空历史，保留当前网关配置"),
        ("/status", "看后端、模型、上下文窗口占用"),
        ("/exit, /q", "退出"),
    )
    width = max(len(name) for name, _ in rows)
    lines = [palette.bold("指令")]
    lines.extend(f"  {palette.cyan(name.ljust(width))}  {palette.dim(text)}" for name, text in rows)
    lines.append(palette.dim("直接输入文字就是发给模型的消息；Ctrl+C 中断本轮，Ctrl+D 退出。"))
    return "\n".join(lines)


def _status_text(session: ChatSession) -> str:
    conversation = session.conversation
    return "\n".join(
        (
            f"后端 {session.backend.name} · 目标 {session.backend.target}",
            f"轮次 {len(conversation.turns)}/{conversation.max_turns} · "
            f"窗口 {conversation.window_chars()}/{conversation.max_context_chars} 字",
            f"思维链显示 {session.show_thinking} · 流式 {session.streaming}",
        )
    )


def print_error(exc: StrataError, palette: Palette) -> None:
    """一行诊断 + 一行可操作提示。提示只在这里给：引擎不猜命令行参数名。"""
    summary = f"{type(exc).__name__}: {exc.describe()}"
    sys.stderr.write(palette.red(f"✗ {summary}") + "\n")
    hint = _error_hint(exc)
    if hint:
        sys.stderr.write(palette.dim(f"  ↳ {hint}") + "\n")


def _error_hint(exc: StrataError) -> str:
    match exc:
        case ProviderAuthError():
            return "本地 Strata 网关的 api-key 随便填；远端用 --api-key 或 STRATA_API_KEY。"
        case ProviderTimeoutError():
            return "先 /retry 一次；模型换入显存或思维链过长都会超时，可加大 --timeout。"
        case ProviderUnreachableError():
            return "确认网关在跑：--base-url / STRATA_BASE_URL；纯离线演示用 --backend local。"
        case ProviderRateLimitError():
            return "已按退避重试过；--max-retries 0 可以直接失败，或稍后再来。"
        case ProviderHTTPError() as http:
            return f"HTTP {http.status_code}：核对 --base-url 与 --model 是否指对了端点。"
        case ProviderResponseError():
            return "网关返回的结构不认识：多半是 --base-url 指到了别的协议端点。"
        case ContextOverflowError():
            return "/clear 清空历史，或加大 --max-context-chars。"
        case NothingToRetryError():
            return "先发一句话，再用 /retry 重放。"
        case CommandError():
            return "/help 看全部指令。"
        case _:
            return ""


# --------------------------------------------------------------------------- 交互指令


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


def _handle_command(text: str, session: ChatSession, palette: Palette) -> Outcome:
    head, _, rest = text[1:].partition(" ")
    match head.lower():
        case "exit" | "quit" | "q" | "bye":
            return "exit"
        case "help" | "h" | "?":
            print(_help_text(palette))
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
            print(_status_text(session))
        case _:
            raise CommandError(f"未知指令 /{head}", command=head)
    return "continue"


# --------------------------------------------------------------------------- 聊天主循环


def run_chat(
    args: argparse.Namespace, settings: ChatSettings, session: ChatSession, palette: Palette
) -> int:
    """REPL：读一行 → 是指令就处理，是话就问答。业务决策都在引擎里，这里只跑腿。"""
    print(_banner(session, settings, palette))
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
                if _handle_command(text, session, palette) == "exit":
                    return EXIT_OK
            else:
                session.ask(text)
        except StrataError as exc:
            print_error(exc, palette)
        except OSError as exc:  # 终端 I/O 故障：管道被关、控制台不可用
            sys.stderr.write(palette.red(f"✗ {type(exc).__name__}: {exc}\n"))
            return EXIT_FAILURE


# --------------------------------------------------------------------------- 参数与入口


def _add_chat_arguments(sub: argparse.ArgumentParser) -> None:
    gateway = sub.add_argument_group("模型与网关")
    gateway.add_argument(
        "--base-url",
        default=None,
        help=f"OpenAI 兼容网关根地址（默认 {DEFAULT_BASE_URL}，可用 STRATA_BASE_URL）",
    )
    gateway.add_argument(
        "--model", default=None, help=f"模型名（默认 {DEFAULT_MODEL}，可用 STRATA_MODEL）"
    )
    gateway.add_argument(
        "--api-key",
        default=None,
        help=f"Bearer 令牌；本地网关接受任意串（默认 {DEFAULT_API_KEY}，可用 STRATA_API_KEY）",
    )
    gateway.add_argument(
        "--backend",
        choices=BACKEND_CHOICES,
        default="strata",
        help="strata=走网关流式；local=离线兜底（默认 strata）",
    )
    gateway.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="读流超时秒数（默认 %(default)s）",
    )
    gateway.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help="还没吐字之前的退避重试次数（默认 %(default)s）",
    )
    gateway.add_argument("--temperature", type=float, default=None, help="透传给网关的采样温度")
    gateway.add_argument("--max-tokens", type=int, default=None, help="透传给网关的最大生成 token")

    memory = sub.add_argument_group("多轮上下文记忆")
    memory.add_argument(
        "--system",
        default=None,
        help="系统提示词（默认工程助手人设，可用 STRATA_SYSTEM_PROMPT）",
    )
    memory.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help="回灌的最大轮数（默认 %(default)s）",
    )
    memory.add_argument(
        "--max-context-chars",
        type=int,
        default=DEFAULT_MAX_CONTEXT_CHARS,
        help="回灌的最大字符预算（默认 %(default)s）",
    )

    output = sub.add_argument_group("输出与运行方式")
    output.add_argument(
        "--thinking",
        choices=("show", "hide"),
        default="show",
        help="是否显示 🧠 Thinking 区块（默认 show，可运行时 /thinking 切换）",
    )
    output.add_argument(
        "--no-stream", action="store_true", help="整段收完再打印（默认逐 token 流式）"
    )
    output.add_argument(
        "--no-color", action="store_true", help="关闭 ANSI 颜色（管道、重定向、录屏场景）"
    )
    output.add_argument("--prompt", default=None, help="非交互模式：只发这一条消息，打印完就退出")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strata", description="Strata 工程命令行入口")
    parser.add_argument(
        "--version",
        action="store_true",
        help="打印包版本与当前 Python 运行时信息",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="命令")
    _add_chat_arguments(
        subparsers.add_parser("chat", help="进入终端聊天：流式 + 🧠 思维链拦截 + 多轮记忆")
    )
    return parser


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
        session = assemble(settings, palette)
    except ConfigurationError as exc:
        print_error(exc, palette)
        return EXIT_CONFIG

    try:
        return run_chat(args, settings, session, palette)
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
