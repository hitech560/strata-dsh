"""Strata 共享内核：值对象、领域事件与端口（Protocol）。

只依赖标准库与 :mod:`strata.errors`；具体实现（HTTP 客户端、终端渲染）不进这里。
"""

from strata.core.chat import (
    AnswerDelta,
    ChatBackend,
    ChatMessage,
    Lane,
    LanePainter,
    MessageRole,
    StreamEvent,
    StreamResult,
    ThinkingDelta,
    Turn,
)
from strata.core.text import clip

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
    "clip",
]
