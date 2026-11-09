"""提供方实现：OpenAI 兼容网关的流式客户端 + 离线兜底后端。

只用标准库（``urllib`` + 手工 SSE 解析），不引三方 SDK。协议事实与帧序规范见
``docs/gateway-stream-protocol.md``。错误一律翻译成 :class:`strata.errors.ProviderError`
家族：上层靠 ``retryable`` / ``retry_after`` / ``status_code`` 决策，不解析异常文本。
"""

import contextlib
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager

from strata.core import AnswerDelta, ChatMessage, StreamEvent, ThinkingDelta, Usage, UsageEvent
from strata.core.text import clip
from strata.errors import (
    ConfigurationError,
    ProviderAuthError,
    ProviderError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderUnreachableError,
)

type ResponseLines = AbstractContextManager[Iterable[bytes]]
"""一次流式响应：可迭代的一行行字节，且必须能当上下文管理器用。"""

type LineOpener = Callable[[urllib.request.Request, float], ResponseLines]
"""注入点：测试用假 opener 喂脚本化帧序，生产用 :func:`default_opener`。"""

DEFAULT_BASE_URL = "http://127.0.0.1:47831/v1"
DEFAULT_MODEL = "iq3_s"
DEFAULT_API_KEY = "strata-local"
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 2

__all__ = [
    "DEFAULT_API_KEY",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "GatewayBackend",
    "LineOpener",
    "LocalBackend",
    "ResponseLines",
    "default_opener",
]


def default_opener(request: urllib.request.Request, timeout: float) -> ResponseLines:
    return urllib.request.urlopen(request, timeout=timeout)


def _mapping(value: object) -> dict[str, object] | None:
    """把外部 JSON 安全收窄成 ``dict[str, object]``；不是对象就返回 ``None``。"""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return None


class GatewayBackend:
    """OpenAI 兼容 Chat Completions 的流式客户端。

    已经向用户吐过字的流**绝不自动重放**（会重复输出）；只有还没出声时，
    才按 ``retryable`` 做带上抖动的指数退避重试。
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
        include_usage: bool = True,
        opener: LineOpener | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: Callable[[str], None] | None = None,
    ) -> None:
        if not base_url.strip():
            raise ConfigurationError("base_url 不能为空")
        if not model.strip():
            raise ConfigurationError("model 不能为空")
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.include_usage = include_usage
        self.endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self.target = self.endpoint
        self._opener = opener if opener is not None else default_opener
        self._sleep = sleep
        self._on_retry = on_retry

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
                if self._on_retry is not None:
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
        body = json.dumps(payload, ensure_ascii=False).encode()
        return urllib.request.Request(self.endpoint, data=body, headers=headers)

    def _payload(self, messages: Sequence[ChatMessage]) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "stream": True,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
        }
        if self.include_usage:
            # 实测：网关认这个开关，把 usage/timings 挂在最后一帧上（见协议文档样本）
            payload["stream_options"] = {"include_usage": True}
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
                    context={"line_index": index, "raw": clip(data, 120)},
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
                "网关在流里回了错误", context={"error": clip(str(event["error"]), 200)}
            )
        choices = event.get("choices")
        if choices:
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
        usage = _usage_from(event.get("usage"), event.get("timings"))
        if usage is not None:
            yield UsageEvent(usage)

    def _map_http_error(self, exc: urllib.error.HTTPError) -> ProviderError:
        status = exc.code
        body = clip(_read_error_body(exc), 300)
        context: dict[str, object] = {"status": status, "endpoint": self.endpoint}
        if body:
            context["body"] = body
        if status in (401, 403):
            return ProviderAuthError(
                "网关拒绝了凭据",
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
                "网关找不到这个模型端点",
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
                f"读流超时（>{self.timeout:g}s）：模型可能还在生成思维链",
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
        """服务端点名要等多久就等多久；否则指数退避加 ±20% 抖动防同期惊群。"""
        if isinstance(exc, ProviderRateLimitError) and exc.retry_after is not None:
            return exc.retry_after
        base = min(8.0, 0.5 * 2 ** (attempt - 1))
        return base * random.uniform(0.8, 1.2)


def _wrapped_timeout(exc: OSError) -> bool:
    """``urllib`` 会把真正的超时包进 ``URLError.reason``，这里把两种形态都认下来。"""
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


def _as_int(value: object) -> int:
    """外部 JSON 里的计数：只认真整数（``bool`` 也是 int 子类，先挡掉）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _usage_from(raw_usage: object, raw_timings: object) -> Usage | None:
    """把网关的 ``usage`` + ``timings`` 合成一份 :class:`~strata.core.Usage`。

    实测键名：``prompt_tokens`` / ``completion_tokens`` / ``total_tokens`` /
    ``prompt_tokens_details.cached_tokens``，速度在 ``timings.predicted_per_second``。
    """
    usage = _mapping(raw_usage)
    if usage is None:
        return None
    details = _mapping(usage.get("prompt_tokens_details")) or {}
    timings = _mapping(raw_timings) or {}
    return Usage(
        input_tokens=_as_int(usage.get("prompt_tokens")),
        output_tokens=_as_int(usage.get("completion_tokens")),
        total_tokens=_as_int(usage.get("total_tokens")),
        cached_input_tokens=_as_int(details.get("cached_tokens")),
        output_per_second=_as_float(timings.get("predicted_per_second")),
    )


class LocalBackend:
    """离线兜底后端：不联网，用可读的思维链 + 结构化回显把整条链路跑通。"""

    name: str = "local"
    target: str = "offline"

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[StreamEvent]:
        asked = [message for message in messages if message.role == "user"]
        question = asked[-1].content if asked else ""
        prior = len(asked) - 1
        steps = (
            f"用户在问：{clip(question, 80)}",
            f"上下文里有 {prior} 轮历史、{len(messages)} 条消息。",
            "离线兜底后端不联网，只做结构化回显。",
        )
        for step in steps:
            yield ThinkingDelta(f"{step}\n")
        yield AnswerDelta(
            f"[local] 已收到：{clip(question, 200)}\n"
            f"（历史 {prior} 轮；把后端切到 strata 就能接真实模型）"
        )
