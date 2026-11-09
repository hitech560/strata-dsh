"""Strata 聊天业务模块：多轮上下文记忆、🧠 思维链拦截与提供方客户端。

依赖方向（见 ``docs/architecture.md``）：只依赖 :mod:`strata.core` 与
:mod:`strata.errors`，不认识命令行与终端。终端交互、上色、参数解析在 :mod:`strata.cli`。

公共 API 在此显式导出，测试只依赖这些导出。
"""

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
    LineOpener,
    LocalBackend,
    ResponseLines,
    default_opener,
)
from strata.chat.session import (
    BACKEND_CHOICES,
    ChatSession,
    ChatSettings,
    build_backend,
    validate_settings,
)
from strata.chat.thinking import (
    ANSWER_HEADER,
    THINKING_END,
    THINKING_HEADER,
    ThinkingInterceptor,
)

__all__ = [
    "ANSWER_HEADER",
    "BACKEND_CHOICES",
    "DEFAULT_API_KEY",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_CONTEXT_CHARS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MODEL",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_TIMEOUT",
    "THINKING_END",
    "THINKING_HEADER",
    "ChatSession",
    "ChatSettings",
    "Conversation",
    "GatewayBackend",
    "LineOpener",
    "LocalBackend",
    "ResponseLines",
    "ThinkingInterceptor",
    "build_backend",
    "default_opener",
    "validate_settings",
    "validate_window",
]
