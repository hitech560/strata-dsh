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
.\.venv\Scripts\strata.exe chat         # 终端聊天：流式 + 🧠 思维链拦截 + 多轮记忆
.\.venv\Scripts\strata.exe chat --help  # 全部参数（网关、上下文预算、色道、非交互）
.\scripts\uv.ps1 run strata chat        # 同一个入口，走 uv（缓存留在 D 盘）
.\scripts\uv.ps1 run pytest             # 只跑测试
.\scripts\uv.ps1 run strata --version   # 打印版本与运行时
.\scripts\uv.ps1 add httpx              # 添加运行时依赖
.\scripts\uv.ps1 add --dev mypy         # 添加开发依赖
.\scripts\uv.ps1 lock --upgrade         # 升级锁
```

## 目录结构

```
pyproject.toml         依赖、工具链（ruff / ty / pytest / coverage）配置
uv.lock                锁定文件（提交进版本库）
.python-version        钉 3.15
scripts/uv.ps1         uv 包装器（缓存与解释器留在 D 盘）
scripts/check.ps1      统一门禁
src/strata/            包本体
  __init__.py          公共 API 与 __all__（只导出版本与错误语义，保持轻量）
  errors.py            异常层级（业务异常一律派生自 StrataError；含提供方/传输语义类）
  core/                共享内核：值对象、端口（ChatBackend / LanePainter）、文本工具
  chat/                业务模块：多轮上下文记忆 + 🧠 思维链拦截 + 网关流式客户端 + 离线兜底
  cli.py / __main__.py 控制台入口：参数解析、环境变量覆盖、装配、REPL
  py.typed             对外声明类型可用
tests/                 pytest，只依赖公共 API
  test_architecture.py 依赖方向守卫（把 docs/architecture.md 的约定跑成测试）
docs/architecture.md         分层与依赖方向约定
docs/gateway-stream-protocol.md  流式网关协议实测记录（SSE 帧序、思维链字段、重试纪律）
```

## 聊天 CLI（`strata chat`）

- 默认接 Strata 本地网关：`--base-url http://127.0.0.1:47831/v1`、`--model iq3_s`、
  `--api-key strata-local`（本地任意串都行）；三个都能用环境变量
  `STRATA_BASE_URL` / `STRATA_MODEL` / `STRATA_API_KEY` 覆盖。
- 流式：`delta.reasoning_content` 拦成独立的 🧠 Thinking 区块（暗色），正文照常逐 token；
  `--thinking hide` 隐藏但仍留档，`/thinking on|off` 运行时切换。
- 多轮记忆：`--max-turns`（默认 12）与 `--max-context-chars`（默认 12000）双重上限，
  谁先到按谁裁剪；思维链不回灌模型。`/history`、`/retry`、`/clear`、`/status` 见 `/help`。
- `--backend local` 离线兜底；`--prompt "…"` 非交互；`--no-color` 给管道/重定向。
- 退出码：`0` 正常 / `1` 运行错 / `2` 配置错 / `130` 中断。

> `src/strata/*_one.py` 是手工保存的旧实现副本，已从 ruff / ty / coverage 范围排除，
> 别 import、也别指望它跟得上分层演进。

## 加第一个业务模块

1. `src/strata/<module>/__init__.py` 建子包，公共 API 在 `__init__.py` 显式导出。
2. 业务规则留在该子包内；跨模块复用的东西下沉到 `strata.core`。
3. `tests/test_<module>.py` 写测试，只 import 公共 API。
4. 需要新三方依赖时用 `.\scripts\uv.ps1 add <pkg>`，并把 `uv.lock` 一起提交。
