"""依赖方向守卫：把 ``docs/architecture.md`` 的约定变成可执行的检查。

约定：``strata.core`` 不依赖任何业务模块；业务模块只依赖 ``strata.core``、
``strata.errors`` 与自己，不直接 import 别的业务模块，也不 import 进程入口 ``strata.cli``；
顶层 ``strata/__init__.py`` 保持轻量，不拉起业务模块。
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "strata"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


def _strata_imports(package: str) -> set[str]:
    collected: set[str] = set()
    for path in sorted((SRC / package).rglob("*.py")):
        collected.update(name for name in _imported_modules(path) if name.startswith("strata"))
    return collected


def test_core_does_not_depend_on_business_layers() -> None:
    offenders = {
        name
        for name in _strata_imports("core")
        if not (name == "strata.errors" or name.startswith("strata.core"))
    }
    assert not offenders


def test_chat_does_not_import_the_process_entry() -> None:
    assert "strata.cli" not in _strata_imports("chat")


def test_chat_only_relies_on_kernel_and_errors() -> None:
    offenders = {
        name
        for name in _strata_imports("chat")
        if not (
            name == "strata.errors"
            or name.startswith("strata.core")
            or name.startswith("strata.chat")
        )
    }
    assert not offenders


def test_package_root_stays_light() -> None:
    """顶层包只导出版本与错误语义，不把业务模块拖进导入路径。"""
    roots = {name.split(".")[0] for name in _imported_modules(SRC / "__init__.py")}
    assert "chat" not in roots
    assert "core" not in roots


def test_core_stays_dependency_free() -> None:
    """共享内核只用标准库与 strata.errors（三方依赖不进内核）。"""
    stdlib_ok = {"collections", "dataclasses", "typing", "types"}
    offenders: set[str] = set()
    for path in sorted((SRC / "core").rglob("*.py")):
        for name in _imported_modules(path):
            head = name.split(".")[0]
            if head != "strata" and head not in stdlib_ok:
                offenders.add(name)
    assert not offenders


@pytest.mark.parametrize(
    ("layer", "forbidden"),
    [("core", "strata.chat"), ("core", "strata.cli"), ("chat", "strata.cli")],
)
def test_layering_guards_cover_the_documented_edges(layer: str, forbidden: str) -> None:
    assert forbidden not in _strata_imports(layer)
