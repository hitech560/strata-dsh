# Strata

uv 管理的 Python 工程，src 布局，Python 3.15。

## 环境事实

| 项目 | 位置 |
| --- | --- |
| 运行时 | CPython 3.15.1（uv 托管），虚拟环境 `./.venv`（D 盘） |
| 依赖声明 / 锁 | `pyproject.toml` + `uv.lock` |
| uv 下载缓存 | `D:\Projects\Strata\uv-cache`（由 `scripts/uv.ps1` 注入，不落 C 盘） |
| uv 托管解释器安装目录 | `D:\Projects\Strata\uv-python`（同上） |

直接敲 `uv ...` 也能跑，但缓存会回到 C 盘的 `AppData\Local\uv`，所以这个项目统一走 `scripts\uv.ps1`。

## 常用命令

```powershell
.\scripts\check.ps1                     # 格式检查 + lint + 类型检查 + 测试（带覆盖率）
.\scripts\uv.ps1 run pytest             # 只跑测试
.\scripts\uv.ps1 run strata --version   # 打印版本与运行时
.\scripts\uv.ps1 add httpx              # 添加运行时依赖
.\scripts\uv.ps1 add --dev mypy         # 添加开发依赖
.\scripts\uv.ps1 lock --upgrade         # 升级锁
```

## 目录结构

```
pyproject.toml         依赖、工具链（ruff / pytest / coverage）配置
uv.lock                锁定文件（提交进版本库）
.python-version        钉 3.15
scripts/uv.ps1         uv 包装器（缓存与解释器留在 D 盘）
scripts/check.ps1      统一门禁
src/strata/            包本体
  __init__.py          公共 API 与 __all__
  errors.py            异常层级（业务异常一律派生自 StrataError）
  core/                共享内核：值对象、领域事件、端口（Protocol）
  cli.py / __main__.py 控制台入口
  py.typed             对外声明类型可用
tests/                 pytest，只依赖公共 API
```

## 加第一个业务模块

1. `src/strata/<module>/__init__.py` 建子包，公共 API 在 `__init__.py` 显式导出。
2. 业务规则留在该子包内；跨模块复用的东西下沉到 `strata.core`。
3. `tests/test_<module>.py` 写测试，只 import 公共 API。
4. 需要新三方依赖时用 `.\scripts\uv.ps1 add <pkg>`，并把 `uv.lock` 一起提交。
