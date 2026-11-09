"""聊天领域的共享内核：值对象与端口（Protocol）。

端口在这里定义，实现（网关流式客户端、离线兜底）留在业务模块 ``strata.chat``，
由装配层 ``strata.cli`` 注入。本模块不认识 HTTP、终端、也不引任何三方类型，
符合 ``docs/architecture.md`` 的依赖方向约定。
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

type MessageRole = Literal["system", "user", "assistant"]
"""进入上下文的角色。"""

type Lane = Literal["thinking", "answer", "notice", "warn", "muted"]
"""输出色道：引擎只说“这是什么语义的一段文本”，怎么上色由装配层决定。"""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """一条进入上下文的消息。"""

    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """思维链增量（OpenAI 兼容网关的 ``delta.reasoning_content``）。"""

    text: str


@dataclass(frozen=True, slots=True)
class AnswerDelta:
    """正文增量（OpenAI 兼容网关的 ``delta.content``）。"""

    text: str


type StreamEvent = ThinkingDelta | AnswerDelta
"""提供方向上吐出的流式事件。"""


@dataclass(slots=True)
class Turn:
    """一轮问答。``thinking`` 只留档供人查看，不回灌模型。"""

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


class ChatBackend(Protocol):
    """提供方端口：把上下文换成一串流式事件。

    实现必须是惰性产出（生成器），否则流式与中断语义都会失效。
    """

    name: str
    target: str

    def stream(self, messages: Sequence[ChatMessage]) -> Iterator[StreamEvent]: ...


class LanePainter(Protocol):
    """按 :data:`Lane` 渲染一段文本；引擎不关心终端上色细节。"""

    def __call__(self, text: str, lane: Lane) -> str: ...


__all__ = [
    "AnswerDelta",
    "ChatBackend",
    "ChatMessage",
    "Lane",
    "LanePainter",
    "MessageRole",
    "StreamEvent",
    "StreamResult",
    "ThinkingDelta",
    "Turn",
]
