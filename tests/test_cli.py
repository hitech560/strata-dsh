import pytest

import strata
from strata.cli import main


def test_version_flag_prints_runtime(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert f"strata {strata.__version__}" in out
    assert "python " in out


def test_default_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: strata" in capsys.readouterr().out


def test_unknown_flag_exits_with_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--nope"])
    assert excinfo.value.code == 2
