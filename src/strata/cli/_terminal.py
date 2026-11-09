"""终端层：ANSI 能力、色道翻译、屏幕文案与错误诊断输出。

这里只放“在终端上怎么呈现”的东西：业务语义（哪些文本属于哪条色道、窗口还剩多少、
token 账目）都由 :mod:`strata.chat` 给出，这一层把它们渲染成人能读的一行。
"""

import os
import sys
from collections.abc import Callable

from strata.chat import ChatSession, ChatSettings, TranscriptLog
from strata.core import Lane, LanePainter
from strata.errors import (
    CommandError,
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
    """装配点：把 :class:`PalettePainter` 作为 :class:`~strata.core.LanePainter` 注入。"""
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


def render_banner(
    session: ChatSession,
    settings: ChatSettings,
    palette: Palette,
    transcript: TranscriptLog | None = None,
) -> str:
    """开场三行：接的是谁、窗口多大、指令在哪。"""
    thinking = "显示" if session.show_thinking else "隐藏"
    stream = "逐 token" if session.streaming else "整段"
    head = palette.bold(f"Strata 聊天 · 模型 {settings.model} · 后端 {session.backend.name}")
    lines = [
        head + palette.dim(f" · 目标 {session.backend.target}"),
        palette.dim(
            f"上下文 {settings.max_turns} 轮 / {settings.max_context_chars} 字 · "
            f"🧠 Thinking {thinking} · 流式 {stream}"
        ),
    ]
    if transcript is not None:
        lines.append(palette.dim(f"会话转录 {transcript.describe()}"))
    lines.append(palette.dim("指令 /help · 退出 /exit"))
    return "\n".join(lines)


def render_help(palette: Palette) -> str:
    rows = (
        ("/help, /h", "看这份指令表"),
        ("/history", "看历史轮次（含每轮思维链摘要）"),
        ("/thinking [on|off]", "切换 🧠 Thinking 是否显示"),
        ("/retry, /r", "重放上一轮输入（改口重问）"),
        ("/clear", "清空历史，保留当前网关配置"),
        ("/status", "看后端、模型、窗口占用、token 账目、转录"),
        ("/session", "看转录文件路径与读写计数"),
        ("/exit, /q", "退出"),
    )
    width = max(len(name) for name, _ in rows)
    lines = [palette.bold("指令")]
    lines.extend(f"  {palette.cyan(name.ljust(width))}  {palette.dim(text)}" for name, text in rows)
    lines.append(palette.dim("直接输入文字就是发给模型的消息；Ctrl+C 中断本轮，Ctrl+D 退出。"))
    return "\n".join(lines)


def render_status(session: ChatSession, transcript: TranscriptLog | None = None) -> str:
    """``/status`` 的正文：连接、窗口、色道、token 账目、转录。"""
    conversation = session.conversation
    lines = [
        f"后端 {session.backend.name} · 目标 {session.backend.target}",
        f"轮次 {len(conversation.turns)}/{conversation.max_turns} · "
        f"窗口 {conversation.window_chars()}/{conversation.max_context_chars} 字",
        f"思维链显示 {session.show_thinking} · 流式 {session.streaming}",
        f"用量 {session.usage.describe()}",
    ]
    lines.append(f"转录 {transcript.describe()}" if transcript else "转录 未启用（--session 可开）")
    return "\n".join(lines)


def render_session(transcript: TranscriptLog | None) -> str:
    if transcript is None:
        return "未启用；启动时加 --session 路径.jsonl 就能跨进程续聊"
    return transcript.describe()


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
