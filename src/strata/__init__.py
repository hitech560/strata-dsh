"""Strata 业务模块基座。

约定：
* ``strata.core`` 放共享内核（值对象、领域事件、端口 Protocol），不放具体业务规则。
* 每个业务模块是 ``src/strata/<module>/`` 下的一个子包，只依赖 ``strata.core`` 与自己；
  业务模块之间不直接 import。
* 公共 API 在包的 ``__init__.py`` 里显式导出，测试只依赖公共 API。
"""

from strata.errors import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
    StrataError,
    ValidationError,
)

__version__ = "0.1.0"

__all__ = [
    "ConfigurationError",
    "ConflictError",
    "NotFoundError",
    "StrataError",
    "ValidationError",
    "__version__",
]
