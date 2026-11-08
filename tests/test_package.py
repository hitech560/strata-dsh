import strata
import strata.errors as errors


def test_version_is_three_part_numeric() -> None:
    parts = strata.__version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_error_hierarchy() -> None:
    for exc in (
        errors.ConfigurationError,
        errors.ValidationError,
        errors.NotFoundError,
        errors.ConflictError,
    ):
        assert issubclass(exc, strata.StrataError)


def test_validation_error_carries_field_and_context() -> None:
    err = errors.ValidationError("bad input", field="age", context={"min": 0})
    assert err.field == "age"
    assert err.context["min"] == 0
    assert str(err) == "bad input"
