"""聊天引擎的行为测试：上下文记忆、🧠 思维链拦截、SSE 解析与错误翻译、会话编排。

只走 :mod:`strata.core` 与 :mod:`strata.chat` 的公共 API，不联网：网关层用假 opener 喂
脚本化 SSE 帧，会话层用假后端喂事件。终端相关的断言在 ``tests/test_cli.py``。
"""

import http.client
import io
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Sequence

import pytest

import strata.chat as chat
from strata.core import (
    AnswerDelta,
    ChatMessage,
    Lane,
    StreamEvent,
    ThinkingDelta,
)
from strata.errors import (
    ConfigurationError,
    ContextOverflowError,
    NothingToRetryError,
    ProviderAuthError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
    ProviderTransportError,
    ProviderUnreachableError,
)

# --------------------------------------------------------------------------- 测试替身


class _ScriptBackend:
    """按脚本吐事件的假后端，顺带记录每次收到的上下文。"""

    name = "script"
    target = "script://test"

    def __init__(self, factory: Callable[[int], Iterable[StreamEvent]]) -> None:
        self._factory = factory
        self.calls: list[list[ChatMessage]] = []

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[StreamEvent]:
        self.calls.append(list(messages))
        yield from self._factory(len(self.calls))


class _PlainPainter:
    """记录每条文本落在哪个语义色道，文本原样放行。"""

    def __init__(self) -> None:
        self.lanes: list[Lane] = []

    def __call__(self, text: str, lane: Lane) -> str:
        self.lanes.append(lane)
        return text


class _Sink:
    """收 writer 输出的假流，用于断言分区输出与刷写节奏。"""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.flushes = 0

    def write(self, text: str) -> None:
        self.chunks.append(text)

    def flush(self) -> None:
        self.flushes += 1

    @property
    def text(self) -> str:
        return "".join(self.chunks)


class _ScriptedResponse:
    """可当上下文管理器迭代的假 HTTP 响应；可在吐完后再抛错模拟断流。"""

    def __init__(self, lines: list[bytes], *, fail: BaseException | None = None) -> None:
        self._lines = lines
        self._fail = fail

    def __enter__(self) -> _ScriptedResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def __iter__(self) -> Iterator[bytes]:
        yield from self._lines
        if self._fail is not None:
            raise self._fail


class _OpenerScript:
    """假 opener：按次序吐出预设结果，异常就抛出；最后一个结果可重复使用。"""

    def __init__(self, outcomes: Sequence[object]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> chat.ResponseLines:
        self.requests.append(request)
        self.timeouts.append(timeout)
        outcome = self.outcomes[min(len(self.requests) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, _ScriptedResponse):
            return outcome
        assert isinstance(outcome, list)
        return _ScriptedResponse(outcome)


def _chunk(**delta: str) -> dict[str, object]:
    return {"choices": [{"index": 0, "delta": dict(delta), "finish_reason": None}]}


def _sse(*chunks: object, done: bool = True) -> list[bytes]:
    """拼出真实网关的帧序：注释行 + data 行 + [DONE]（见 docs/gateway-stream-protocol.md）。"""
    lines = [": keep-alive\n"]
    lines += [f"data: {json.dumps(chunk, ensure_ascii=False)}\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n")
    return [line.encode() for line in lines]


def _http_error(
    code: int, *, retry_after: str | None = None, body: bytes = b"gateway busy"
) -> urllib.error.HTTPError:
    headers = http.client.HTTPMessage()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "http://h/v1/chat/completions", code, "boom", headers, io.BytesIO(body)
    )


def _gateway(
    opener: _OpenerScript,
    *,
    max_retries: int = 0,
    sleep: Callable[[float], None] = lambda _seconds: None,
    on_retry: Callable[[str], None] = lambda _notice: None,
) -> chat.GatewayBackend:
    return chat.GatewayBackend(
        base_url="http://h/v1",
        model="iq3_s",
        api_key="strata-local",
        max_retries=max_retries,
        sleep=sleep,
        on_retry=on_retry,
        opener=opener,
    )


def _interceptor(
    sink: _Sink,
    painter: _PlainPainter | None = None,
    *,
    show_thinking: bool = True,
    streaming: bool = True,
) -> chat.ThinkingInterceptor:
    return chat.ThinkingInterceptor(
        painter=painter if painter is not None else _PlainPainter(),
        write=sink.write,
        flush=sink.flush,
        show_thinking=show_thinking,
        streaming=streaming,
    )


# --------------------------------------------------------------------------- 上下文记忆


def test_messages_keep_system_first_and_newest_last() -> None:
    conversation = chat.Conversation("SYS", max_turns=5, max_context_chars=4000)
    conversation.add_turn("第一问", "第一答", "第一思路")
    messages = conversation.messages_for("第二问")
    assert [message.role for message in messages] == ["system", "user", "assistant", "user"]
    assert messages[0].content == "SYS"
    assert messages[-1].content == "第二问"
    joined = " ".join(message.content for message in messages)
    assert "第一答" in joined
    assert "第一思路" not in joined  # 思维链不回灌模型


def test_turn_window_drops_oldest_first() -> None:
    conversation = chat.Conversation("S", max_turns=2, max_context_chars=4000)
    for index in range(4):
        conversation.add_turn(f"u{index}", f"a{index}")
    assert [turn.user for turn in conversation.turns] == ["u2", "u3"]
    contents = [message.content for message in conversation.messages_for("u4")]
    assert "u1" not in contents
    assert "u3" in contents


def test_char_budget_drops_old_turns_but_keeps_the_latest() -> None:
    conversation = chat.Conversation("S", max_turns=10, max_context_chars=120)
    conversation.add_turn("x" * 60, "y" * 60)
    conversation.add_turn("最近的问", "最近的答")
    contents = [message.content for message in conversation.messages_for("新的问")]
    assert "x" * 60 not in contents
    assert "最近的问" in contents
    assert "新的问" in contents


def test_turn_without_answer_is_not_echoed_back() -> None:
    """思维链把生成预算用尽的那一轮：只回灌用户侧，不塞空的 assistant 消息。"""
    conversation = chat.Conversation("S", max_turns=5, max_context_chars=4000)
    conversation.add_turn("只想到一半的问题", "", "想了一大堆但没吐正文")
    messages = conversation.messages_for("下一问")
    assert [message.role for message in messages] == ["system", "user", "user"]
    assert "只想到一半的问题" in [message.content for message in messages]
    assert all(message.content for message in messages)


def test_overflow_when_input_alone_exceeds_budget() -> None:
    conversation = chat.Conversation("S" * 10, max_turns=5, max_context_chars=64)
    with pytest.raises(ContextOverflowError) as excinfo:
        conversation.messages_for("z" * 60)
    assert excinfo.value.size == 60
    assert excinfo.value.limit == 54


def test_window_bounds_are_validated() -> None:
    with pytest.raises(ConfigurationError):
        chat.Conversation("S", max_turns=0)
    with pytest.raises(ConfigurationError):
        chat.Conversation("S", max_context_chars=32)
    with pytest.raises(ConfigurationError):
        chat.validate_window(max_turns=1, max_context_chars=8)


def test_drop_last_and_clear_drive_retry_and_reset() -> None:
    conversation = chat.Conversation("S")
    with pytest.raises(NothingToRetryError):
        conversation.drop_last()
    conversation.add_turn("u", "a")
    assert conversation.drop_last().user == "u"
    conversation.add_turn("u1", "a1")
    conversation.add_turn("u2", "a2")
    assert conversation.clear() == 2
    assert conversation.turns == ()


def test_render_marks_interrupted_and_hides_thinking_by_default() -> None:
    conversation = chat.Conversation("S")
    conversation.add_turn("问题", "回答", "只留档的思路", interrupted=True)
    plain = conversation.render()
    assert "问题" in plain
    assert "回答" in plain
    assert "已中断" in plain
    assert "只留档的思路" not in plain
    assert "只留档的思路" in conversation.render(include_thinking=True)


# --------------------------------------------------------------------------- 思维链拦截


def test_interceptor_partitions_thinking_before_answer() -> None:
    sink = _Sink()
    painter = _PlainPainter()
    interceptor = _interceptor(sink, painter)
    result = interceptor.consume(
        [ThinkingDelta("推理甲"), ThinkingDelta("推理乙"), AnswerDelta("正文")]
    )
    assert result.thinking == "推理甲推理乙"
    assert result.answer == "正文"
    assert result.interrupted is False
    assert "🧠 Thinking" in sink.text
    assert "🧠 思维链结束" in sink.text
    assert "推理甲" in sink.text.split("正文")[0]  # 思维链先于正文落地
    assert sink.flushes > 1  # 逐 token 真的在刷，不是攒一整段
    assert "thinking" in painter.lanes
    assert "answer" in painter.lanes
    assert "notice" in painter.lanes  # 正文抬头走单独色道


def test_interceptor_hides_thinking_but_still_records_it() -> None:
    sink = _Sink()
    interceptor = _interceptor(sink, show_thinking=False)
    result = interceptor.consume([ThinkingDelta("藏好的思路"), AnswerDelta("答")])
    assert result.thinking == "藏好的思路"
    assert "藏好的思路" not in sink.text
    assert "🧠" not in sink.text
    assert "回答" in sink.text


def test_interceptor_buffers_everything_when_not_streaming() -> None:
    sink = _Sink()
    interceptor = _interceptor(sink, streaming=False)
    interceptor.consume([ThinkingDelta("思"), AnswerDelta("答")])
    assert len(sink.chunks) == 1  # 一次性打印
    assert "🧠 Thinking" in sink.text
    assert "答" in sink.text


def test_interceptor_keeps_partial_output_on_ctrl_c() -> None:
    def events() -> Iterator[StreamEvent]:
        yield AnswerDelta("前半句")
        raise KeyboardInterrupt

    sink = _Sink()
    result = _interceptor(sink).consume(events())
    assert result.interrupted is True
    assert result.answer == "前半句"
    assert "已中断" in sink.text


def test_interceptor_flags_empty_response() -> None:
    sink = _Sink()
    result = _interceptor(sink).consume([])
    assert result.answer == ""
    assert "没有任何输出" in sink.text


def test_interceptor_reports_thinking_only_round() -> None:
    sink = _Sink()
    result = _interceptor(sink).consume([ThinkingDelta("只想不答")])
    assert result.thinking == "只想不答"
    assert result.answer == ""
    assert "只有思维链" in sink.text


# --------------------------------------------------------------------------- 网关协议


def test_gateway_parses_reasoning_and_content_deltas() -> None:
    opener = _OpenerScript([_sse(_chunk(reasoning_content="分两步"), _chunk(content="31.4"))])
    backend = _gateway(opener)
    events = list(backend.stream([ChatMessage(role="user", content="算体积")]))
    assert events == [ThinkingDelta("分两步"), AnswerDelta("31.4")]

    request = opener.requests[0]
    assert request.full_url == "http://h/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer strata-local"
    assert request.get_header("Accept") == "text/event-stream"
    raw_body = request.data if isinstance(request.data, bytes) else b"{}"
    payload = json.loads(raw_body)
    assert payload["stream"] is True
    assert payload["model"] == "iq3_s"
    assert payload["messages"][-1]["content"] == "算体积"
    assert opener.timeouts == [chat.DEFAULT_TIMEOUT]


def test_gateway_accepts_alternate_reasoning_field() -> None:
    opener = _OpenerScript([_sse(_chunk(reasoning="另一种字段"))])
    events = list(_gateway(opener).stream([]))
    assert events == [ThinkingDelta("另一种字段")]


def test_gateway_maps_status_codes_to_error_semantics() -> None:
    with pytest.raises(ProviderAuthError) as excinfo:
        list(_gateway(_OpenerScript([_http_error(401)])).stream([]))
    assert excinfo.value.status_code == 401

    with pytest.raises(ProviderServerError):
        list(_gateway(_OpenerScript([_http_error(503)])).stream([]))


def test_gateway_honours_retry_after_and_replays_before_first_delta() -> None:
    opener = _OpenerScript([_http_error(429, retry_after="0"), _sse(_chunk(content="好了"))])
    slept: list[float] = []
    backend = _gateway(opener, max_retries=2, sleep=slept.append)
    events = list(backend.stream([ChatMessage(role="user", content="再来")]))
    assert events == [AnswerDelta("好了")]
    assert len(opener.requests) == 2
    assert slept == [0.0]  # Retry-After 盖过指数退避


def test_gateway_gives_up_after_max_retries() -> None:
    opener = _OpenerScript([_http_error(500)])
    with pytest.raises(ProviderServerError):
        list(_gateway(opener, max_retries=1).stream([]))
    assert len(opener.requests) == 2


def test_gateway_never_replays_a_stream_that_already_spoke() -> None:
    broken = _ScriptedResponse(
        _sse(_chunk(content="半截话"), done=False), fail=ConnectionResetError("对端复位")
    )
    opener = _OpenerScript([broken])
    backend = _gateway(opener, max_retries=3)
    with pytest.raises(ProviderTransportError):
        list(backend.stream([]))
    assert len(opener.requests) == 1  # 已经吐过字：不重放，避免重复输出


def test_gateway_rejects_malformed_and_in_band_errors() -> None:
    malformed = ["data: {不是 JSON}\n".encode()]
    with pytest.raises(ProviderResponseError):
        list(_gateway(_OpenerScript([malformed])).stream([]))
    in_band = [f"data: {json.dumps({'error': {'message': '模型正在换入显存'}})}\n".encode()]
    with pytest.raises(ProviderResponseError):
        list(_gateway(_OpenerScript([in_band])).stream([]))


def test_gateway_reports_connectivity_failures_as_unreachable() -> None:
    with pytest.raises(ProviderUnreachableError):
        list(_gateway(_OpenerScript([urllib.error.URLError("拒绝连接")])).stream([]))
    with pytest.raises(ProviderTimeoutError):
        list(_gateway(_OpenerScript([TimeoutError("读到一半没动静")])).stream([]))


def test_gateway_rejects_blank_connection_config() -> None:
    with pytest.raises(ConfigurationError):
        chat.GatewayBackend(base_url="  ", model="m")
    with pytest.raises(ConfigurationError):
        chat.GatewayBackend(base_url="http://h/v1", model=" ")


# --------------------------------------------------------------------------- 会话编排


def _session(
    factory: Callable[[int], Iterable[StreamEvent]], *, streaming: bool = True
) -> tuple[chat.ChatSession, _ScriptBackend, _Sink]:
    backend = _ScriptBackend(factory)
    sink = _Sink()
    session = chat.ChatSession(
        backend=backend,
        conversation=chat.Conversation("S", max_turns=4),
        painter=_PlainPainter(),
        write=sink.write,
        flush=sink.flush,
        streaming=streaming,
    )
    return session, backend, sink


def test_session_rolls_history_into_the_next_request() -> None:
    session, backend, _sink = _session(lambda call: [AnswerDelta(f"答{call}")])
    session.ask("第一问")
    session.ask("第二问")
    roles = [message.role for message in backend.calls[1]]
    assert roles == ["system", "user", "assistant", "user"]
    assert backend.calls[1][2].content == "答1"  # 上一轮正文进了下一轮上下文


def test_retry_replays_the_same_user_text_without_growing_history() -> None:
    session, backend, _sink = _session(lambda call: [ThinkingDelta("想"), AnswerDelta(f"答{call}")])
    session.ask("问 A")
    session.ask("问 B")
    session.retry()
    assert len(session.conversation.turns) == 2
    assert backend.calls[-1][-1].content == "问 B"
    assert session.conversation.turns[-1].answer == "答3"
    assert session.conversation.turns[-1].thinking == "想"


def test_settings_validation_covers_sampling_and_window() -> None:
    with pytest.raises(ConfigurationError):
        chat.validate_settings(chat.ChatSettings(temperature=9.0))
    with pytest.raises(ConfigurationError):
        chat.validate_settings(chat.ChatSettings(max_tokens=0))
    with pytest.raises(ConfigurationError):
        chat.validate_settings(chat.ChatSettings(base_url="localhost:8080"))
    with pytest.raises(ConfigurationError):
        chat.validate_settings(chat.ChatSettings(system_prompt="p" * 500, max_context_chars=100))
    with pytest.raises(ConfigurationError):
        chat.validate_settings(chat.ChatSettings(backend="who-knows"))

    fixed = chat.validate_settings(chat.ChatSettings(system_prompt="   "))
    assert fixed.system_prompt == chat.DEFAULT_SYSTEM_PROMPT  # 空人设回填默认人设


def test_build_backend_picks_the_two_ports() -> None:
    assert chat.build_backend(chat.ChatSettings(backend="local")).name == "local"
    gateway = chat.build_backend(chat.ChatSettings(backend="strata", base_url="http://h/v1/"))
    assert gateway.name == "gateway"
    assert gateway.target == "http://h/v1/chat/completions"
