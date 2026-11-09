# 模块边界约定

## 分层

```
strata.core    共享内核：值对象 / 领域事件 / 端口（Protocol）/ 文本工具
strata.<mod>   业务模块，一个模块一个子包（目前落地：strata.chat）
strata.cli     进程入口包：_parser（参数与环境变量）/ _terminal（ANSI 与文案）/ _repl（主循环）
               / __init__（装配与 main）—— 不写业务规则
strata.errors  错误语义（所有业务异常的根）
```

## 依赖方向

- `strata.core` 不依赖任何业务模块，也不依赖进程入口；只用标准库 + `strata.errors`。
- 业务模块只依赖 `strata.core`、`strata.errors` 与自己，**不直接 import 其他业务模块**，
  也**不 import `strata.cli`**；需要协作时通过 `core` 里定义的端口（Protocol）由装配层注入。
- 三方依赖只在业务模块内出现，公共 API 不泄漏三方类型。
- 这些边由 [`tests/test_architecture.py`](../tests/test_architecture.py) 自动守住：
  越界的 import 会在门禁里直接失败，不靠 code review 兜。

当前两个端口（都在 [`src/strata/core/chat.py`](../src/strata/core/chat.py)）：

| 端口 | 语义 | 实现 | 注入点 |
| --- | --- | --- | --- |
| `ChatBackend` | 把上下文换成一串流式事件 | `GatewayBackend`（OpenAI 兼容流式）、`LocalBackend`（离线兜底） | `strata.chat.build_backend` |
| `LanePainter` | 把语义色道渲染成文本 | `cli.PalettePainter`（ANSI） | `cli.assemble` |

引擎只说“这段文本属于哪条色道”（`thinking` / `answer` / `notice` / `warn` / `muted`），
怎么上色、写到哪个流由装配层决定 —— 所以引擎在无终端的地方（测试、重定向、门禁）也能跑。

## 装配层只做这件事

```python
settings = validate_settings(settings_from_args(args))  # 参数 + 环境变量 → 裸配置 → 校验
session = assemble(settings, palette)  # 后端 + 上下文记忆 + 色道 + 输出流
return run_chat(args, settings, session, palette)  # REPL 只跑腿，不做决策
```

## 错误语义

所有业务异常派生自 `strata.errors.StrataError`，带 `context` 只放可序列化的诊断信息，不放敏感数据。
提供方与传输层进一步把故障表达成可判定语义：`retryable`（能否重放整次请求）、
`retry_after`（服务端要求的等待秒数）、`status_code`、`partial`（断流前已交付的内容）——
上层不解析异常文本。**已经向用户吐过字的流绝不自动重放**，细节见
[`docs/gateway-stream-protocol.md`](gateway-stream-protocol.md)。

## 不参与门禁的文件

`src/strata/*_one.py` 是手工保存的旧实现副本，原样留在包里但已从 ruff / ty / coverage 的
范围里排除（见 `pyproject.toml`）。**不要 import 它们**，也别指望它们跟得上分层演进。
