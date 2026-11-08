"""Strata 异常层级：业务异常一律从 :class:`StrataError` 派生。"""

from collections.abc import Mapping
from types import MappingProxyType


class StrataError(Exception):
    """所有 Strata 业务异常的基类。"""

    def __init__(self, message: str = "", *, context: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: Mapping[str, object] = MappingProxyType(dict(context or {}))


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


class NotFoundError(StrataError):
    """按标识查找不到目标实体。"""


class ConflictError(StrataError):
    """与已有状态冲突（重复创建、并发覆盖等）。"""
