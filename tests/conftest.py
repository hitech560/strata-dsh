"""测试夹具。

转录测试要真的往磁盘写。系统临时目录（``%TEMP%``）里早先由非沙箱 pytest 建过的基目录
带着受限令牌读不了的权限项，所以这里把草稿放在工作区根下的 ``.pytest-scratch/``：
本次进程自己建、自己列、自己删，不依赖 pytest 的 basetemp 机制。
"""

import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

SCRATCH = Path(__file__).resolve().parents[1] / ".pytest-scratch"


@pytest.fixture(scope="session", autouse=True)
def _scratch_dir() -> Iterator[Path]:
    """会话级草稿目录：开局先清空上一轮残留，收工再整个删掉。

    这里只删本次进程自己建的东西（受限令牌有删除权），不碰系统临时目录。
    """
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, ignore_errors=True)
    yield SCRATCH
    shutil.rmtree(SCRATCH, ignore_errors=True)


def _drop_if_empty(directory: Path) -> None:
    try:
        if not any(directory.iterdir()):
            directory.rmdir()
    except OSError:
        pass


@pytest.fixture
def transcript_path() -> Iterator[Path]:
    """本次测试独占的一个 ``.jsonl`` 路径；文件与草稿目录用完即删。"""
    path = SCRATCH / f"{uuid4().hex}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
        _drop_if_empty(path.parent)
