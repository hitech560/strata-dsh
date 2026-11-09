"""异常层级的语义测试：上层靠属性决策，不解析异常文本。"""

from collections.abc import MutableMapping
from typing import cast

import pytest

from strata import errors


def test_every_business_error_derives_from_strata_error() -> None:
    for exc in (
        errors.ConfigurationError,
        errors.ValidationError,
        errors.NotFoundError,
        errors.ConflictError,
        errors.CommandError,
        errors.ConversationError,
        errors.ContextOverflowError,
        errors.NothingToRetryError,
        errors.ProviderError,
        errors.ProviderHTTPError,
        errors.ProviderAuthError,
        errors.ProviderRateLimitError,
        errors.ProviderServerError,
        errors.ProviderUnreachableError,
        errors.ProviderTimeoutError,
        errors.ProviderTransportError,
        errors.ProviderResponseError,
        errors.StreamInterruptedError,
    ):
        assert issubclass(exc, errors.StrataError)


def test_unexpected_builtin_names_are_not_shadowed() -> None:
    """异常层不能遮蔽内建异常名，否则传输层判断会串味。"""
    assert "TimeoutError" not in vars(errors)
    assert "ServerError" not in vars(errors)


def test_retryable_flags_drive_backoff_decisions() -> None:
    retryable = (
        errors.ProviderUnreachableError,
        errors.ProviderTimeoutError,
        errors.ProviderTransportError,
        errors.ProviderServerError,
        errors.ProviderRateLimitError,
    )
    for exc in retryable:
        assert exc.retryable is True

    for exc in (
        errors.ProviderError,
        errors.ProviderAuthError,
        errors.ProviderHTTPError,
        errors.ProviderResponseError,
        errors.StreamInterruptedError,
    ):
        assert exc.retryable is False


def test_describe_renders_sorted_pairs() -> None:
    err = errors.NotFoundError("找不到订单", context={"table": "orders", "order_id": 7})
    assert err.describe() == "找不到订单 [order_id=7, table='orders']"

    bare = errors.StrataError()
    assert bare.describe() == "StrataError"


def test_context_is_immutable() -> None:
    payload = {"k": 1}
    err = errors.StrataError("boom", context=payload)
    payload["k"] = 2
    assert err.context["k"] == 1
    smuggled = cast("MutableMapping[str, object]", err.context)
    with pytest.raises(TypeError):
        smuggled["k"] = 3


def test_provider_errors_carry_transport_diagnostics() -> None:
    rate = errors.ProviderRateLimitError("限流", retry_after=1.5, status_code=429)
    assert rate.retry_after == 1.5
    assert rate.status_code == 429

    unreachable = errors.ProviderTimeoutError("超时", url="http://x/y")
    assert unreachable.url == "http://x/y"
    assert isinstance(unreachable, errors.ProviderUnreachableError)

    interrupted = errors.StreamInterruptedError("断流", partial="已经吐了一半")
    assert interrupted.partial == "已经吐了一半"

    overflow = errors.ContextOverflowError("超预算", limit=100, size=140)
    assert (overflow.limit, overflow.size) == (100, 140)
    assert isinstance(overflow, errors.ConversationError)

    command = errors.CommandError("用法错", command="thinking")
    assert command.command == "thinking"
