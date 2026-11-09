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
| 请求体 | `model`、`stream: true`、`messages: [{role, content}, …]`；`temperature` / `max_tokens` 仅在显式给出时才带上；默认还带 `stream_options: {"include_usage": true}`（`--no-usage` 可关） |

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
8. **没有 `choices` 或 `choices` 为空的帧不产出文本**，但同一帧若带 `usage`，
   账目**必须**吃到（这是统计帧的唯一用途），不能整帧跳过。
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

## 5. usage 帧与 token 账目（实测）

请求带上 `stream_options: {"include_usage": true}` 后，**最后一帧**同时带
`finish_reason`、`usage`，外加网关特有的 `timings`（llama.cpp 风格）：

```
data: {…"choices": [{"index":0,"delta":{},"finish_reason":"stop"}],
       "usage": {"prompt_tokens":55,"completion_tokens":16,"total_tokens":71,
                 "prompt_tokens_details":{"cached_tokens":0}},
       "timings": {"cache_n":0,"prompt_n":55,"prompt_ms":2239.2,"prompt_per_token_ms":40.713,
                   "prompt_per_second":24.6,"predicted_n":16,"predicted_ms":904.6,
                   "predicted_per_token_ms":56.538,"predicted_per_second":17.7,
                   "draft_n":11,"draft_n_accepted":6}}
data: [DONE]
```

字段映射（`GatewayBackend._usage_from` → :class:`strata.core.Usage`）：

| 网关字段 | 落到 | 说明 |
| --- | --- | --- |
| `prompt_tokens` | `Usage.input_tokens` | 回灌的上下文大小，`/status` 的窗口预算按它核对 |
| `completion_tokens` | `Usage.output_tokens` | **含思维链**：推理模型的思考就是生成的一部分 |
| `total_tokens` | `Usage.total_tokens` | |
| `prompt_tokens_details.cached_tokens` | `Usage.cached_input_tokens` | 前缀缓存命中 |
| `timings.predicted_per_second` | `Usage.output_per_second` | 生成速度；没有就不显示 |

三条纪律：

- usage 帧里的 `delta` 是空对象 → 不产出文本；收尾仍然只认 `[DONE]`，别把它当结束信号。
- 另一类实现把 usage 放在 `choices: []` 的独立尾帧里 → 两种形态都认。
- `--no-usage` 留给对未知字段严格的网关；关掉后 `/status` 老实显示「用量 未上报」。

页脚每轮带 `tokens 55→16 · 17.7 token/s`，`/status` 打跨轮累计（只统计真的回过 usage 的轮次）。
顺带一条实测教训：**思维链吃紧时会把 `max_tokens` 用光**，正文 0 字（`finish_reason":"length"`），
所以 `--max-tokens` 对推理模型要给得比直觉更大。

## 6. 怎么自己复现一帧

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
