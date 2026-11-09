"""会话装配：配置校验、后端选择、单轮问答的编排。

这一层把 :mod:`strata.chat` 的零件（上下文记忆、提供方、思维链拦截）串成一次会话。
它不认识命令行，也不认识终端：参数解析、环境变量覆盖、上色与读写留在 ``strata.cli``。
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from strata.chat.conversation import (
    DEFAULT_MAX_CONTEXT_CHARS,
    DEFAULT_MAX_TURNS,
    DEFAULT_SYSTEM_PROMPT,
    Conversation,
    validate_window,
)
from strata.chat.providers import (
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    GatewayBackend,
    LocalBackend,
)
from strata.chat.thinking import ThinkingInterceptor
from strata.core import ChatBackend, LanePainter, StreamResult, Turn, Usage
from strata.errors import ConfigurationError

BACKEND_CHOICES = ("strata", "local")

__all__ = [
    "BACKEND_CHOICES",
    "ChatSession",
    "ChatSettings",
    "build_backend",
    "validate_settings",
]


@dataclass(frozen=True, slots=True)
class ChatSettings:
    """一次聊天进程的完整配置：装配层解析出来，交给引擎校验并使用。"""

    backend: str = "strata"
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = DEFAULT_API_KEY
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    max_turns: int = DEFAULT_MAX_TURNS
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    show_thinking: bool = True
    streaming: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    request_usage: bool = True
    session_file: str | None = None
    """JSONL 转录路径：给了就在启动时恢复历史，并在每轮后追加。"""


def validate_settings(settings: ChatSettings) -> ChatSettings:
    """把裸配置校验成可用配置；不可用就抛 :class:`~strata.errors.ConfigurationError`。"""
    if not settings.base_url.startswith(("http://", "https://")):
        raise ConfigurationError(
            "base_url 需要以 http:// 或 https:// 开头", context={"base_url": settings.base_url}
        )
    if not settings.model.strip():
        raise ConfigurationError("model 不能为空")
    if settings.timeout <= 0:
        raise ConfigurationError("timeout 必须是正数", context={"timeout": settings.timeout})
    if settings.max_retries < 0:
        raise ConfigurationError(
            "max_retries 不能为负", context={"max_retries": settings.max_retries}
        )
    if settings.temperature is not None and not 0.0 <= settings.temperature <= 2.0:
        raise ConfigurationError(
            "temperature 需要在 0 到 2 之间", context={"temperature": settings.temperature}
        )
    if settings.max_tokens is not None and settings.max_tokens < 1:
        raise ConfigurationError("max_tokens 至少为 1", context={"max_tokens": settings.max_tokens})
    if settings.backend not in BACKEND_CHOICES:
        raise ConfigurationError("未知后端", context={"backend": settings.backend})
    if settings.session_file is not None and not settings.session_file.strip():
        raise ConfigurationError("session 文件路径不能为空")
    if not settings.system_prompt.strip():
        settings = replace(settings, system_prompt=DEFAULT_SYSTEM_PROMPT)
    validate_window(
        max_turns=settings.max_turns,
        max_context_chars=settings.max_context_chars,
    )
    if len(settings.system_prompt) >= settings.max_context_chars:
        raise ConfigurationError(
            "系统提示词已经吃满上下文预算",
            context={
                "system_prompt_chars": len(settings.system_prompt),
                "max_context_chars": settings.max_context_chars,
            },
        )
    return settings


def build_backend(settings: ChatSettings) -> ChatBackend:
    """按配置挑提供方；两个分支都满足 :class:`~strata.core.ChatBackend` 端口。"""
    if settings.backend == "local":
        return LocalBackend()
    return GatewayBackend(
        base_url=settings.base_url,
        model=settings.model,
        api_key=settings.api_key,
        timeout=settings.timeout,
        max_retries=settings.max_retries,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        include_usage=settings.request_usage,
    )


class UsageLedger:
    """跨轮 token 账目：只累计网关真的上报过 usage 的那些轮次。"""

    def __init__(self) -> None:
        self.calls = 0
        self.total: Usage | None = None

    def add(self, usage: Usage | None) -> None:
        if usage is None:
            return
        self.calls += 1
        base = self.total or Usage(input_tokens=0, output_tokens=0, total_tokens=0)
        self.total = Usage(
            input_tokens=base.input_tokens + usage.input_tokens,
            output_tokens=base.output_tokens + usage.output_tokens,
            total_tokens=base.total_tokens + usage.total_tokens,
            cached_input_tokens=base.cached_input_tokens + usage.cached_input_tokens,
            output_per_second=(
                usage.output_per_second
                if usage.output_per_second is not None
                else base.output_per_second
            ),
        )

    def describe(self) -> str:
        """一行摘要，给 ``/status`` 用。"""
        if self.total is None:
            return "未上报（网关没回 usage 帧）"
        speed = (
            f" · {self.total.output_per_second:.1f} token/s"
            if self.total.output_per_second is not None
            else ""
        )
        cached = (
            f" · 缓存命中 {self.total.cached_input_tokens}"
            if self.total.cached_input_tokens
            else ""
        )
        return (
            f"{self.total.input_tokens}→{self.total.output_tokens} tokens"
            f"（合计 {self.total.total_tokens}）{cached}{speed} · 已计 {self.calls} 轮"
        )


class ChatSession:
    """一次会话：上下文记忆 + 提供方 + 思维链拦截的编排。"""

    def __init__(
        self,
        *,
        backend: ChatBackend,
        conversation: Conversation,
        painter: LanePainter,
        write: Callable[[str], None],
        flush: Callable[[], None],
        streaming: bool = True,
        show_thinking: bool = True,
        on_turn: Callable[[Turn, Usage | None], None] | None = None,
    ) -> None:
        self.backend = backend
        self.conversation = conversation
        self.painter = painter
        self.write = write
        self.flush = flush
        self.streaming = streaming
        self.show_thinking = show_thinking
        self.on_turn = on_turn
        self.usage = UsageLedger()

    def ask(self, text: str) -> StreamResult:
        messages = self.conversation.messages_for(text)
        interceptor = ThinkingInterceptor(
            painter=self.painter,
            write=self.write,
            flush=self.flush,
            show_thinking=self.show_thinking,
            streaming=self.streaming,
        )
        result = interceptor.consume(self.backend.stream(messages))
        turn = self.conversation.add_turn(
            text,
            result.answer,
            result.thinking,
            interrupted=result.interrupted,
        )
        self.usage.add(result.usage)
        if self.on_turn is not None:
            self.on_turn(turn, result.usage)
        return result

    def retry(self) -> StreamResult:
        """把上一轮输入原样重放（改口重问），历史长度不增长。"""
        last = self.conversation.drop_last()
        return self.ask(last.user)
