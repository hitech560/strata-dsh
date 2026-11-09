"""命令行入口：``strata`` 控制台脚本与 ``python -m strata``。

``strata chat`` 是终端聊天主程序，三件事：

* **流式对接 OpenAI 兼容网关**（默认 Strata 本地网关 ``http://127.0.0.1:47831/v1``）。
  实测协议：SSE，事件行 ``data: {json}``，注释行 ``: keep-alive``，``data: [DONE]`` 收尾；
  思维链在 ``choices[0].delta.reasoning_content``，正文在 ``choices[0].delta.content``。
  只用标准库（``urllib``），不引三方 SDK。
* **🧠 Thinking 思维链拦截**：把 ``reasoning_content`` 增量导到独立的暗色区块，与正文分区
  输出、分区留档；只有正文回灌进下一轮上下文（推理类网关只出不进）。
* **多轮上下文记忆**：滚动窗口由「轮数」和「字符预算」双重上限控制，思维链只留档不回灌。

``--backend local`` 是不联网的兜底后端，断网、跑门禁、写测试都用它。
"""

import argparse
import contextlib
import json
import os
import platform
import random
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from strata import __version__
from errors import (
    CommandError,
    ConfigurationError,
    ContextOverflowError,
    NothingToRetryError,
    ProviderAuthError,
    ProviderError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderUnreachableError,
    StrataError,
)

# --------------------------------------------------------------------------- 常量

DEFAULT_BASE_URL = "http://127.0.0.1:47831/v1"
DEFAULT_MODEL = "iq3_s"
DEFAULT_API_KEY = "strata-local"
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_CONTEXT_CHARS = 12_000
DEFAULT_SYSTEM_PROMPT = (
    "你是 Strata，跑在终端里的工程助手。回答直接、可落地，默认中文；"
    "涉及代码时给可粘贴的完整片段，不写无关注释。"
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 130

_THINKING_HEADER = "🧠 Thinking ▾ "
_ANSWER_HEADER = "🤖 回答 ▸ "
_PROMPT = "你 ▸ "


def _clip(text: str, limit: int) -> str:
    """压成一行并截断，用于历史列表和错误摘要的展示。"""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return f"{flat[: max(0, limit - 1)]}…"


def _mapping(value: object) -> dict[str, object] | None:
    """把外部 JSON 安全收窄成 ``dict[str, object]``；不是对象就返回 ``None``。"""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return None


# --------------------------------------------------------------------------- 值对象

type MessageRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条进入上下文的消息。"""

    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """思维链增量（网关的 ``reasoning_content``）。"""

    text: str


@dataclass(frozen=True, slots=True)
class AnswerDelta:
    """正文增量（网关的 ``content``）。"""

    text: str


type StreamEvent = ThinkingDelta | AnswerDelta


@dataclass(slots=True)
class Turn:
    """一轮问答；``thinking`` 只留档，不回灌模型。"""

    user: str
    answer: str
    thinking: str = ""
    interrupted: bool = False


@dataclass(frozen=True, slots=True)
class StreamResult:
    """一次流式消费的汇总结果。"""

    answer: str
    thinking: str
    elapsed: float
    interrupted: bool


# --------------------------------------------------------------------------- 端口


class ChatBackend(Protocol):
    """提供方端口：由装配层注入，会话与渲染层不认识具体实现。"""

    name: str
    target: str

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[StreamEvent]: ...


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

    def green(self, text: str) -> str:
        return self._wrap("32", text)


def resolve_palette(*, no_color: bool) -> Palette:
    """按显式开关和环境变量决定要不要上色；非 TTY 一律不上色。"""
    if no_color:
        return Palette(enabled=False)
    if os.environ.get("FORCE_COLOR"):
        return Palette(enabled=True)
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return Palette(enabled=False)
    return Palette(enabled=sys.stdout.isatty())


# --------------------------------------------------------------------------- 上下文记忆


def validate_window(*, max_turns: int, max_context_chars: int) -> None:
    """窗口上限的单一出处：配置校验与 :class:`Conversation` 都走这里。"""
    if max_turns < 1:
        raise ConfigurationError("--max-turns 至少为 1", context={"max_turns": max_turns})
    if max_context_chars < 64:
        raise ConfigurationError(
            "--max-context-chars 至少为 64",
            context={"max_context_chars": max_context_chars},
        )


class Conversation:
    """多轮对话的上下文记忆。

    ``max_turns`` 与 ``max_context_chars`` 是双重上限，谁先到按谁裁剪，且永远保留最近一轮。
    思维链存进 :class:`Turn` 供 ``/history`` 查看，但不回灌：推理类网关的
    ``reasoning_content`` 只出不进，回灌既贵又会被模型当成正文续写。
    """

    def __init__(
        self,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    ) -> None:
        validate_window(max_turns=max_turns, max_context_chars=max_context_chars)
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.max_context_chars = max_context_chars
        self._turns: list[Turn] = []

    @property
    def turns(self) -> tuple[Turn, ...]:
        return tuple(self._turns)

    def add_turn(
        self,
        user: str,
        answer: str,
        thinking: str = "",
        *,
        interrupted: bool = False,
    ) -> Turn:
        turn = Turn(user=user, answer=answer, thinking=thinking, interrupted=interrupted)
        self._turns.append(turn)
        overflow = len(self._turns) - self.max_turns
        if overflow > 0:
            del self._turns[:overflow]
        return turn

    def drop_last(self) -> Turn:
        if not self._turns:
            raise NothingToRetryError("还没有可重放的对话轮次")
        return self._turns.pop()

    def clear(self) -> int:
        removed = len(self._turns)
        self._turns.clear()
        return removed

    def messages_for(self, user_text: str) -> list[ChatMessage]:
        """本轮请求的上下文：system + 装得下的历史轮次 + 本轮输入（新的在最后）。"""
        budget = self.max_context_chars - len(self.system_prompt)
        if len(user_text) > budget:
            raise ContextOverflowError(
                "本轮输入超出上下文预算，请精简后重发",
                limit=budget,
                size=len(user_text),
                context={"max_context_chars": self.max_context_chars},
            )
        used = len(user_text)
        kept: list[ChatMessage] = []
        for turn in reversed(self._turns):
            cost = len(turn.user) + len(turn.answer)
            if used + cost > budget:
                break
            used += cost
            kept.insert(0, ChatMessage(role="assistant", content=turn.answer))
            kept.insert(0, ChatMessage(role="user", content=turn.user))
        return [
            ChatMessage(role="system", content=self.system_prompt),
            *kept,
            ChatMessage(role="user", content=user_text),
        ]

    def window_chars(self, user_text: str = "") -> int:
        """当前窗口实际占用字符数（含本轮输入，不含 system）。"""
        used = len(user_text)
        for turn in reversed(self._turns):
            cost = len(turn.user) + len(turn.answer)
            if used + cost > self.max_context_chars:
                break
            used += cost
        return used

    def render(self, *, include_thinking: bool = False, limit: int = 120) -> str:
        if not self._turns:
            return "（当前没有历史轮次）"
        lines: list[str] = []
        for index, turn in enumerate(self._turns, start=1):
            marker = " ·已中断" if turn.interrupted else ""
            lines.append(f"[{index}/{self.max_turns}] 你 ▸ {_clip(turn.user, limit)}{marker}")
            if include_thinking and turn.thinking:
                lines.append(f"            🧠 {_clip(turn.thinking, limit)}")
            lines.append(f"            🤖 {_clip(turn.answer, limit)}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- 思维链拦截


class ThinkingInterceptor:
    """把 :data:`StreamEvent` 流拦截成分区输出。

    思维链走暗色 + ``🧠 Thinking`` 抬头，与正文完全分开；两者各自留档，
    只有正文进入下一轮上下文。``streaming=False`` 时先收全再一次性打印。
    Ctrl+C 中断会把已经吐出的内容收进结果，不丢半截。
    """

    def __init__(
        self,
        palette: Palette,
        *,
        show_thinking: bool = True,
        streaming: bool = True,
        write: Callable[[str], None] | None = None,
        flush: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.palette = palette
        self.show_thinking = show_thinking
        self.streaming = streaming
        self._write = write if write is not None else sys.stdout.write
        self._flush = flush if flush is not None else sys.stdout.flush
        self._clock = clock
        self._buffer: list[str] = []
        self._phase = "idle"

    def consume(self, events: Iterable[StreamEvent]) -> StreamResult:
        started = self._clock()
        thinking: list[str] = []
        answer: list[str] = []
        interrupted = False
        try:
            for event in events:
                if isinstance(event, ThinkingDelta):
                    self._on_thinking(event.text, thinking)
                else:
                    self._on_answer(event.text, answer)
        except KeyboardInterrupt:
            interrupted = True
            self._emit(self.palette.yellow("\n⏸ 已中断，保留已产出的内容\n"))
        self._finish(answer, thinking, self._clock() - started)
        return StreamResult(
            answer="".join(answer),
            thinking="".join(thinking),
            elapsed=self._clock() - started,
            interrupted=interrupted,
        )

    def _on_thinking(self, text: str, sink: list[str]) -> None:
        if not text:
            return
        sink.append(text)
        if self._phase == "idle":
            self._phase = "thinking"
            if self.show_thinking:
                self._emit("\n" + self.palette.dim(_THINKING_HEADER))
        if self.show_thinking:
            self._emit(self.palette.dim(text))

    def _on_answer(self, text: str, sink: list[str]) -> None:
        if not text:
            return
        sink.append(text)
        if self._phase != "answer":
            if self._phase == "thinking" and self.show_thinking:
                self._emit(self.palette.dim("\n🧠 思维链结束\n"))
            self._phase = "answer"
            self._emit(self.palette.cyan(_ANSWER_HEADER))
        self._emit(text)

    def _finish(self, answer: list[str], thinking: list[str], elapsed: float) -> None:
        if self._phase == "thinking":
            self._emit(self.palette.dim("\n🧠 思维链结束\n"))
        if not answer and not thinking:
            self._emit(self.palette.yellow("（模型没有任何输出）\n"))
        elif not answer:
            self._emit(
                self.palette.yellow("\n（本轮只有思维链，没有正文；换 --thinking hide 可隐藏）\n")
            )
        else:
            self._emit("\n")
        sizes = f"正文 {len(''.join(answer))} 字 · 思维链 {len(''.join(thinking))} 字"
        self._emit(self.palette.dim(f"· {sizes} · 耗时 {elapsed:.1f}s\n"))
        if not self.streaming:
            self._write("".join(self._buffer))
            self._buffer.clear()
        self._flush()

    def _emit(self, text: str) -> None:
        if self.streaming:
            self._safe_write(text)
            self._flush()
            return
        self._buffer.append(text)

    def _safe_write(self, text: str) -> None:
        try:
            self._write(text)
        except UnicodeEncodeError:
            # 老终端（GBK 之类）画不出 emoji / 上标：降级成本地编码能表示的文本
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            self._write(text.encode(encoding, "replace").decode(encoding, "replace"))


# --------------------------------------------------------------------------- 网关后端


type ResponseLines = AbstractContextManager[Iterable[bytes]]
type LineOpener = Callable[[urllib.request.Request, float], ResponseLines]


def default_opener(request: urllib.request.Request, timeout: float) -> ResponseLines:
    return urllib.request.urlopen(request, timeout=timeout)


class GatewayBackend:
    """OpenAI 兼容 Chat Completions 的流式客户端，只用标准库。

    错误一律翻译成 :class:`strata.errors.ProviderError` 家族：上层靠 ``retryable`` /
    ``retry_after`` 决策，不去解析异常文本。已经向用户吐过字的流绝不自动重放，
    避免重复输出。
    """

    name: str = "gateway"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = DEFAULT_API_KEY,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        temperature: float | None = None,
        max_tokens: int | None = None,
        opener: LineOpener | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: Callable[[str], None] | None = None,
    ) -> None:
        if not base_url.strip():
            raise ConfigurationError("--base-url 不能为空")
        if not model.strip():
            raise ConfigurationError("--model 不能为空")
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self.target = self.endpoint
        self._opener = opener if opener is not None else default_opener
        self._sleep = sleep
        self._on_retry = on_retry if on_retry is not None else _print_retry_notice

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[StreamEvent]:
        payload = self._payload(messages)
        attempt = 0
        while True:
            produced = False
            try:
                for event in self._attempt(payload):
                    produced = True
                    yield event
                return
            except ProviderError as exc:
                if produced or not exc.retryable or attempt >= self.max_retries:
                    raise
                attempt += 1
                delay = self._backoff(attempt, exc)
                self._on_retry(
                    f"{type(exc).__name__}：第 {attempt}/{self.max_retries} 次重试，"
                    f"{delay:.1f}s 后重放整次请求"
                )
                self._sleep(delay)

    def _attempt(self, payload: dict[str, object]) -> Iterator[StreamEvent]:
        request = self._build_request(payload)
        try:
            with self._opener(request, self.timeout) as response:
                yield from self._read_sse(response)
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc) from exc
        except OSError as exc:
            raise self._map_transport_error(exc) from exc

    def _build_request(self, payload: dict[str, object]) -> urllib.request.Request:
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(self.endpoint, data=body, headers=headers)

    def _payload(self, messages: Sequence[ChatMessage]) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "stream": True,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        return payload

    def _read_sse(self, response: Iterable[bytes]) -> Iterator[StreamEvent]:
        for index, raw in enumerate(response):
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue  # 空行与 : keep-alive 注释
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                return
            try:
                chunk = json.loads(data)
            except ValueError as exc:
                raise ProviderResponseError(
                    "流式响应里有一行不是合法 JSON",
                    context={"line_index": index, "raw": _clip(data, 120)},
                ) from exc
            yield from self._events_from(chunk)

    def _events_from(self, chunk: object) -> Iterator[StreamEvent]:
        event = _mapping(chunk)
        if event is None:
            raise ProviderResponseError(
                "流式响应事件不是对象", context={"type": type(chunk).__name__}
            )
        if event.get("error") is not None:
            raise ProviderResponseError(
                "网关在流里回了错误", context={"error": _clip(str(event["error"]), 200)}
            )
        choices = event.get("choices")
        if not choices:
            return  # 只有 usage / 心跳的收尾事件
        if not isinstance(choices, list):
            raise ProviderResponseError("流式响应的 choices 不是数组")
        first = _mapping(choices[0])
        if first is None:
            raise ProviderResponseError("流式响应缺少 choices[0] 对象")
        delta = _mapping(first.get("delta")) or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            yield ThinkingDelta(reasoning)
        content = delta.get("content")
        if isinstance(content, str) and content:
            yield AnswerDelta(content)

    def _map_http_error(self, exc: urllib.error.HTTPError) -> ProviderError:
        status = exc.code
        body = _clip(_read_error_body(exc), 300)
        context: dict[str, object] = {"status": status, "endpoint": self.endpoint}
        if body:
            context["body"] = body
        if status in (401, 403):
            return ProviderAuthError(
                "网关拒绝了凭据：检查 --api-key / STRATA_API_KEY",
                status_code=status,
                url=self.endpoint,
                body=body,
                context=context,
            )
        if status == 429:
            return ProviderRateLimitError(
                "网关限流",
                status_code=status,
                url=self.endpoint,
                body=body,
                retry_after=_retry_after(exc),
                context=context,
            )
        if status >= 500:
            return ProviderServerError(
                f"网关内部错误 {status}",
                status_code=status,
                url=self.endpoint,
                body=body,
                context=context,
            )
        if status == 404:
            return ProviderHTTPError(
                "网关找不到这个模型端点：检查 --base-url / --model",
                status_code=status,
                url=self.endpoint,
                body=body,
                context=context,
            )
        return ProviderHTTPError(
            f"网关返回 {status}", status_code=status, url=self.endpoint, body=body, context=context
        )

    def _map_transport_error(self, exc: OSError) -> ProviderError:
        context: dict[str, object] = {"endpoint": self.endpoint, "reason": str(exc)}
        if isinstance(exc, TimeoutError) or _wrapped_timeout(exc):
            return ProviderTimeoutError(
                f"读流超时（>{self.timeout:g}s）：模型可能还在生成思维链，可加大 --timeout",
                url=self.endpoint,
                context=context,
            )
        if isinstance(exc, ConnectionError):
            return ProviderTransportError(
                "连接被中断：网关可能刚重启或模型正在换入显存",
                url=self.endpoint,
                context=context,
            )
        return ProviderUnreachableError(f"连不上网关：{exc}", url=self.endpoint, context=context)

    def _backoff(self, attempt: int, exc: ProviderError) -> float:
        if isinstance(exc, ProviderRateLimitError) and exc.retry_after is not None:
            return exc.retry_after
        base = min(8.0, 0.5 * 2 ** (attempt - 1))
        return base * random.uniform(0.8, 1.2)


def _print_retry_notice(text: str) -> None:
    sys.stderr.write(f"↻ {text}\n")


def _wrapped_timeout(exc: OSError) -> bool:
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    with contextlib.suppress(OSError, ValueError):
        raw = exc.read()
        if raw:
            return raw.decode("utf-8", "replace")
    return ""


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    headers = exc.headers
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except TypeError, ValueError:
        return None


class LocalBackend:
    """离线兜底后端：不联网，用可读的思维链 + 结构化回显把整条链路跑通。"""

    name: str = "local"
    target: str = "offline"

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[StreamEvent]:
        asked = [message for message in messages if message.role == "user"]
        question = asked[-1].content if asked else ""
        prior = len(asked) - 1
        steps = (
            f"用户在问：{_clip(question, 80)}",
            f"上下文里有 {prior} 轮历史、{len(messages)} 条消息。",
            "离线兜底后端不联网，只做结构化回显。",
        )
        for step in steps:
            yield ThinkingDelta(f"{step}\n")
        yield AnswerDelta(
            f"[local] 已收到：{_clip(question, 200)}\n"
            f"（历史 {prior} 轮；要用真实模型改成 --backend strata）"
        )


# --------------------------------------------------------------------------- 装配


@dataclass(frozen=True, slots=True)
class ChatSettings:
    """一次聊天进程的完整配置（命令行 + 环境变量解析后的结果）。"""

    backend: str = "strata"
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = DEFAULT_API_KEY
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_turns: int = DEFAULT_MAX_TURNS
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    show_thinking: bool = True
    streaming: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    prompt: str | None = None


def validate_settings(settings: ChatSettings) -> ChatSettings:
    """把裸配置校验成可用配置；不可用就抛 :class:`ConfigurationError`。"""
    if not settings.base_url.startswith(("http://", "https://")):
        raise ConfigurationError(
            "--base-url 需要以 http:// 或 https:// 开头", context={"base_url": settings.base_url}
        )
    if not settings.model.strip():
        raise ConfigurationError("--model 不能为空")
    if settings.timeout <= 0:
        raise ConfigurationError("--timeout 必须是正数", context={"timeout": settings.timeout})
    if settings.max_retries < 0:
        raise ConfigurationError(
            "--max-retries 不能为负", context={"max_retries": settings.max_retries}
        )
    if settings.temperature is not None and not 0.0 <= settings.temperature <= 2.0:
        raise ConfigurationError(
            "--temperature 需要在 0 到 2 之间", context={"temperature": settings.temperature}
        )
    if settings.max_tokens is not None and settings.max_tokens < 1:
        raise ConfigurationError(
            "--max-tokens 至少为 1", context={"max_tokens": settings.max_tokens}
        )
    if settings.backend not in ("strata", "local"):
        raise ConfigurationError("未知后端", context={"backend": settings.backend})
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
    )


class ChatSession:
    """一次会话：把后端、上下文记忆、思维链拦截装配到一起。"""

    def __init__(
        self,
        *,
        backend: ChatBackend,
        conversation: Conversation,
        palette: Palette,
        streaming: bool = True,
        show_thinking: bool = True,
    ) -> None:
        self.backend = backend
        self.conversation = conversation
        self.palette = palette
        self.streaming = streaming
        self.show_thinking = show_thinking

    def ask(self, text: str) -> StreamResult:
        messages = self.conversation.messages_for(text)
        interceptor = ThinkingInterceptor(
            self.palette,
            show_thinking=self.show_thinking,
            streaming=self.streaming,
        )
        result = interceptor.consume(self.backend.stream(messages))
        self.conversation.add_turn(
            text,
            result.answer,
            result.thinking,
            interrupted=result.interrupted,
        )
        return result

    def retry(self) -> StreamResult:
        last = self.conversation.drop_last()
        return self.ask(last.user)


# --------------------------------------------------------------------------- 交互指令


type Outcome = Literal["continue", "exit"]


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


def _parse_toggle(rest: str, *, current: bool, command: str) -> bool:
    match rest.strip().lower():
        case "on" | "show":
            return True
        case "off" | "hide":
            return False
        case "":
            return not current
        case other:
            raise CommandError(
                f"/{command} 只接受 on 或 off（收到 {other}）",
                command=command,
            )


def _handle_command(text: str, session: ChatSession, palette: Palette) -> Outcome:
    head, _, rest = text[1:].partition(" ")
    match head.lower():
        case "exit" | "quit" | "q" | "bye":
            return "exit"
        case "help" | "h" | "?":
            print(_help_text(palette))
        case "history" | "hist":
            print(conversation_history(session))
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


def conversation_history(session: ChatSession) -> str:
    return session.conversation.render(include_thinking=session.show_thinking)


# --------------------------------------------------------------------------- 聊天主程序


def settings_from_args(args: argparse.Namespace) -> ChatSettings:
    """命令行优先，其次环境变量，最后默认值。"""
    api_key = args.api_key
    if api_key is None:
        api_key = os.environ.get("STRATA_API_KEY", DEFAULT_API_KEY)
    system_prompt = args.system or os.environ.get("STRATA_SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT
    return ChatSettings(
        backend=args.backend,
        base_url=args.base_url or os.environ.get("STRATA_BASE_URL") or DEFAULT_BASE_URL,
        model=args.model or os.environ.get("STRATA_MODEL") or DEFAULT_MODEL,
        api_key=api_key,
        timeout=args.timeout,
        max_retries=args.max_retries,
        system_prompt=system_prompt,
        max_turns=args.max_turns,
        max_context_chars=args.max_context_chars,
        show_thinking=args.thinking == "show",
        streaming=not args.no_stream,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        prompt=args.prompt,
    )


def _banner(session: ChatSession, settings: ChatSettings, palette: Palette) -> str:
    thinking = "显示" if session.show_thinking else "隐藏"
    stream = "逐 token" if session.streaming else "整段"
    return "\n".join(
        (
            palette.bold(f"Strata 聊天 · 模型 {settings.model} · 后端 {session.backend.name}")
            + palette.dim(f" · 目标 {session.backend.target}"),
            palette.dim(
                f"上下文 {settings.max_turns} 轮 / {settings.max_context_chars} 字 · "
                f"🧠 Thinking {thinking} · 流式 {stream}"
            ),
            palette.dim("指令 /help · 退出 /exit"),
        )
    )


def print_error(exc: StrataError, palette: Palette) -> None:
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
            return "先 /retry 一次；模型换入显存或思维链太长都会超时，可加大 --timeout。"
        case ProviderUnreachableError():
            return "确认网关在跑：--base-url / STRATA_BASE_URL；纯离线演示用 --backend local。"
        case ProviderRateLimitError():
            return "已按退避重试过；--max-retries 0 可以直接失败，或稍后再来。"
        case ProviderResponseError():
            return "网关返回的结构不认识：贴一下 --base-url 与 --model，多半是地址指错了。"
        case ContextOverflowError():
            return "/clear 清空历史，或加大 --max-context-chars。"
        case NothingToRetryError():
            return "先发一句话，再用 /retry 重放。"
        case CommandError():
            return "/help 看全部指令。"
        case _:
            return ""


def run_chat(args: argparse.Namespace, palette: Palette) -> int:
    """``strata chat`` 主循环：读输入 → 装配上下文 → 流式问答 + 思维链拦截。"""
    settings = validate_settings(settings_from_args(args))
    backend = build_backend(settings)
    conversation = Conversation(
        settings.system_prompt,
        max_turns=settings.max_turns,
        max_context_chars=settings.max_context_chars,
    )
    session = ChatSession(
        backend=backend,
        conversation=conversation,
        palette=palette,
        streaming=settings.streaming,
        show_thinking=settings.show_thinking,
    )
    print(_banner(session, settings, palette))

    if settings.prompt is not None:
        session.ask(settings.prompt)
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
            # 💡 核心修复：添加无斜杠的经典退出命令拦截（quit 和 exit）
            if text.lower() in ["quit", "exit"]:
                print(palette.cyan("👋 [System] 会话已结束。本地内存已完全释放。"))
                return EXIT_OK
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


def _add_chat_arguments(chat: argparse.ArgumentParser) -> None:
    gateway = chat.add_argument_group("模型与网关")
    gateway.add_argument(
        "--base-url",
        default=None,
        help=f"OpenAI 兼容网关根地址（默认 {DEFAULT_BASE_URL}，可用 STRATA_BASE_URL）",
    )
    gateway.add_argument(
        "--model",
        default=None,
        help=f"模型名（默认 {DEFAULT_MODEL}，可用 STRATA_MODEL）",
    )
    gateway.add_argument(
        "--api-key",
        default=None,
        help=f"Bearer 令牌；本地网关接受任意串（默认 {DEFAULT_API_KEY}，可用 STRATA_API_KEY）",
    )
    gateway.add_argument(
        "--backend",
        choices=("strata", "local"),
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

    memory = chat.add_argument_group("多轮上下文记忆")
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

    output = chat.add_argument_group("输出与运行方式")
    output.add_argument(
        "--thinking",
        choices=("show", "hide"),
        default="show",
        help="是否显示 🧠 Thinking 区块（默认 show，可运行时 /thinking 切换）",
    )
    output.add_argument(
        "--no-stream",
        action="store_true",
        help="整段收完再打印（默认逐 token 流式）",
    )
    output.add_argument(
        "--no-color",
        action="store_true",
        help="关闭 ANSI 颜色（管道、重定向、录屏场景）",
    )
    output.add_argument(
        "--prompt",
        default=None,
        help="非交互模式：只发这一条消息，打印完就退出",
    )


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
        return run_chat(args, palette)
    except ConfigurationError as exc:
        print_error(exc, palette)
        return EXIT_CONFIG
    except StrataError as exc:
        print_error(exc, palette)
        return EXIT_FAILURE
    except BrokenPipeError:  # 下游管道提前关闭：不算失败
        with contextlib.suppress(OSError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        return EXIT_OK
    except KeyboardInterrupt:
        print()
        return EXIT_INTERRUPTED

if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        with contextlib.suppress(OSError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        sys.exit(EXIT_OK)
