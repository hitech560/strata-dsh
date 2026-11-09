"""Strata 异常层级：业务异常一律从 :class:`StrataError` 派生。

分层（与 ``docs/architecture.md`` 的错误语义一致）：

* 根：:class:`StrataError` —— ``message`` 加不可变 ``context`` 诊断字典，只放可序列化、
  不含敏感数据的字段。
* 配置与输入：:class:`ConfigurationError`、:class:`ValidationError`。
* 状态与指令：:class:`NotFoundError`、:class:`ConflictError`、:class:`CommandError`。
* 会话：:class:`ConversationError` 及其派生（上下文超限、无历史可重放）。
* 提供方与传输：:class:`ProviderError` 及其派生。

上层不必解析异常文本就能做决策：``retryable`` 决定是否可以退避重试，
``retry_after`` 给出服务端要求的等待秒数，``status_code`` 定位 HTTP 语义，
``partial`` 交出流式中断前已经产出的内容。
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar

__all__ = [
    "CommandError",
    "ConfigurationError",
    "ConflictError",
    "ContextOverflowError",
    "ConversationError",
    "NotFoundError",
    "ProviderAuthError",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "ProviderServerError",
    "ProviderTimeoutError",
    "ProviderTransportError",
    "ProviderUnreachableError",
    "StrataError",
    "StreamInterruptedError",
    "ValidationError",
]


class StrataError(Exception):
    """所有 Strata 业务异常的基类。"""

    def __init__(self, message: str = "", *, context: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: Mapping[str, object] = MappingProxyType(dict(context or {}))

    def describe(self) -> str:
        """单行人类可读摘要：消息 + 按 key 排序的 ``context``。"""
        if not self.context:
            return self.message or type(self).__name__
        pairs = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message or type(self).__name__} [{pairs}]"


# --------------------------------------------------------------------------- 配置与输入


class ConfigurationError(StrataError):
    """配置缺失、冲突或非法。"""


class ValidationError(StrataError):
    """输入或领域数据不满足约束。"""

    def __init__(
        self,
        message: str = "",
        *,
        field: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.field = field


# --------------------------------------------------------------------------- 状态与指令


class NotFoundError(StrataError):
    """按标识查找不到目标实体。"""


class ConflictError(StrataError):
    """与已有状态冲突（重复创建、并发覆盖等）。"""


class CommandError(StrataError):
    """交互式指令用法错误（拼写、参数个数、参数取值）。"""

    def __init__(
        self,
        message: str = "",
        *,
        command: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.command = command


# --------------------------------------------------------------------------- 会话


class ConversationError(StrataError):
    """会话状态不满足某次操作的前提。"""


class ContextOverflowError(ConversationError):
    """待回灌的上下文超出预算，裁剪历史也解决不了。"""

    def __init__(
        self,
        message: str = "",
        *,
        limit: int | None = None,
        size: int | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.limit = limit
        self.size = size


class NothingToRetryError(ConversationError):
    """没有可重放的历史轮次。"""


# --------------------------------------------------------------------------- 提供方与传输


class ProviderError(StrataError):
    """模型提供方 / 传输层异常的基类。

    ``retryable`` 表示重放整次请求是否安全；已经向用户吐过字的流式请求，
    上层不得自动重放（会重复输出），那时统一走 :class:`StreamInterruptedError`。
    """

    retryable: ClassVar[bool] = False


class ProviderUnreachableError(ProviderError):
    """连不上提供方：DNS 失败、连接被拒、TLS 握手失败等。"""

    retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str = "",
        *,
        url: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.url = url


class ProviderTimeoutError(ProviderUnreachableError):
    """在给定时间内没能建连、也没能读到下一个数据块。"""


class ProviderTransportError(ProviderUnreachableError):
    """连接已建立但被底层中断（对端复位、读到一半断流）。"""


class ProviderHTTPError(ProviderError):
    """网关返回非 2xx，且不属于下面更具体的几类。"""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        url: str | None = None,
        body: str = "",
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.status_code = status_code
        self.url = url
        self.body = body


class ProviderServerError(ProviderHTTPError):
    """5xx：提供方自己出故障，重放整次请求是安全的。"""

    retryable: ClassVar[bool] = True


class ProviderAuthError(ProviderHTTPError):
    """401 / 403：凭据缺失或无效，重试无意义。"""


class ProviderRateLimitError(ProviderHTTPError):
    """429：需要按 ``retry_after`` 退避后重试。"""

    retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: float | None = None,
        status_code: int | None = None,
        url: str | None = None,
        body: str = "",
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            url=url,
            body=body,
            context=context,
        )
        self.retry_after = retry_after


class ProviderResponseError(ProviderError):
    """响应结构不合法：不是 JSON、缺字段、流协议跑偏、带内错误。"""


class StreamInterruptedError(ProviderError):
    """流式响应中途断掉；``partial`` 是已经交付给用户的内容。"""

    def __init__(
        self,
        message: str = "",
        *,
        partial: str = "",
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.partial = partial
