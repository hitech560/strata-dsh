# Strata 网关流式协议（实测记录）

> 实测环境：本地 Strata 网关 `http://127.0.0.1:47831/v1`，模型 `iq3_s`
> （`Qwen3.8-Flash-Next IQ3_S`），2026-11-08 压测抓取。客户端是
> [`src/strata/chat/providers.py`](../src/strata/chat/providers.py) 里的 `GatewayBackend`
> （纯 `urllib` + 手工 SSE 解析，无三方 SDK）。
>
> 这份文档是**实现依据**：改客户端前先照这里的帧序核对，别靠猜。

## 1. 端点与请求

| 项目 | 取值 |
| --- | --- |
| 端点 | `POST {base_url}/chat/completions`（`base_url` 末尾 `/` 会被剥掉） |
| 请求头 | `Content-Type: application/json`、`Accept: text/event-stream`、`Authorization: Bearer <token>` |
| 凭据 | 本地网关接受任意非空串；项目默认 `strata-local`（`--api-key` / `STRATA_API_KEY`） |
| 请求体 | `model`、`stream: true`、`messages: [{role, content}, …]`；`temperature` / `max_tokens` 仅在显式给出时才带上 |

`GET {base_url}/models` 可列模型，用于连通性与模型名自检。

## 2. 帧序（SSE，实测原样）

响应 `Content-Type: text/event-stream`，逐行产出：

```
data: {"id":"chatcmpl-…","object":"chat.completion.chunk","created":…,"model":"iq3_s",
       "choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}
: keep-alive
: keep-alive
data: {…,"choices":[{"index":0,"delta":{"reasoning_content":"We"},"finish_reason":null}]}
data: {…,"choices":[{"index":0,"delta":{"reasoning_content":" need"},"finish_reason":null}]}
data: {…,"choices":[{"index":0,"delta":{"content":"体积"},"finish_reason":null}]}
data: [DONE]
```

解析规则（`GatewayBackend._read_sse` / `_events_from` 一一对应）：

1. **只有 `data:` 前缀的行是事件**；去掉 `data:` 后 `strip()`。
2. `data: [DONE]` 是**唯一的正常终止信号**，见到就收流；不要靠 `finish_reason` 收尾。
3. 以 `:` 开头的行是注释（实测是 `: keep-alive`，长思维链期间会连续出现），**必须忽略**，
   不能当数据解析。空行同样忽略。
4. 首个 delta 常见形态是 `{"role":"assistant","content":""}`：空字符串**不产出任何输出**，
   否则会打出一个空的正文抬头。
5. 思维链（🧠 Thinking）在 **`choices[0].delta.reasoning_content`**；为兼容其他
   OpenAI 兼容实现，`delta.reasoning` 也认。两者都没有 → 本轮没有思维链。
6. 正文在 `choices[0].delta.content`。
7. 同一帧里 `reasoning_content` 与 `content` 不会同时非空；出现时按“先思维链后正文”的顺序
   产出（模型切换点的常见约定）。
8. **没有 `choices` 或 `choices` 为空**的帧（只带 `usage` 的收尾统计帧）→ 跳过，不是错误。
9. 带内错误：帧里出现顶层 `error` 对象 → 抛 `ProviderResponseError`，不当正文吐给用户。
10. 一行不是合法 JSON → 抛 `ProviderResponseError`，`context` 里带行号与截断原文。

一次实测样本：圆柱体积题，182 行事件（含 2 行 `: keep-alive` 与 `[DONE]`），
思维链与正文分段清晰，先思考后作答。

## 3. 错误语义与重试策略

`GatewayBackend` 把 HTTP/传输故障翻译成
[`strata.errors`](../src/strata/errors.py) 的语义类，上层只看属性：

| 状况 | 异常 | `retryable` | 备注 |
| --- | --- | --- | --- |
| 401 / 403 | `ProviderAuthError` | ✗ | 检查 `--api-key` / `STRATA_API_KEY` |
| 429 | `ProviderRateLimitError` | ✓ | 读 `Retry-After` 头，服务端说等多久就等多久 |
| 5xx | `ProviderServerError` | ✓ | 未吐过字才重放 |
| 404 | `ProviderHTTPError` | ✗ | 多半 `--base-url` / `--model` 指错 |
| 连不上 / DNS | `ProviderUnreachableError` | ✓ | — |
| 读流超时 | `ProviderTimeoutError` | ✓ | 长思维链容易撞车，调 `--timeout` |
| 中途断流 | `ProviderTransportError` | ✓ | **已吐过字就不重放**，见下 |
| 结构不对 / 带内 error | `ProviderResponseError` | ✗ | — |

重放纪律（重要）：**已经向用户吐出过任何 delta 的流，绝不自动重放**——重放会重复输出。
实现里用 `produced` 标志守住这条，此时直接把异常交给上层，让用户用 `/retry` 显式重来。
未吐字时的退避：`Retry-After` 优先，否则 `min(8s, 0.5·2^(n-1))` 再乘 ±20% 抖动。

## 4. 思维链与上下文记忆的关系

- `reasoning_content` **只出不进**：请求体里不回灌思维链，只有正文进入下一轮上下文。
  回灌既费 token，又会被模型当成正文续写。
- 思维链仍要留档：`Turn.thinking` 保存原文，供 `/history` 与审计查看。
- 思维链长度可观（实测一轮 150–210 字，复杂题上千字），所以
  `--max-context-chars` 的预算只算正文，不算思维链。

## 5. 怎么自己复现一帧

```powershell
# 列模型
Invoke-RestMethod http://127.0.0.1:47831/v1/models -Headers @{Authorization='Bearer strata-local'}
```

```powershell
# 直接看原始帧序
.\.venv\Scripts\strata.exe chat --max-tokens 400   # 界面会分区打印 🧠 与正文
```

跑测试不需要真网关：`tests/test_chat.py` 用假 opener 喂脚本化 SSE 帧
（`: keep-alive` → `data:` → `[DONE]`），把协议细节锁在测试里。
