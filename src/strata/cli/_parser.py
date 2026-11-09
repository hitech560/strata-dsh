"""参数面：argparse 定义 + 环境变量覆盖，产出一份引擎裸配置。

优先级：命令行 > 环境变量 > 引擎默认值。这里**不做**合法性判断——校验归
:func:`strata.chat.validate_settings`，避免参数面和业务规则各写一套。
"""

import argparse
import os

from strata.chat import (
    BACKEND_CHOICES,
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_CONTEXT_CHARS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_TURNS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    ChatSettings,
)


def settings_from_args(args: argparse.Namespace) -> ChatSettings:
    """把解析结果 + 环境变量拼成 :class:`~strata.chat.ChatSettings`。"""
    api_key = args.api_key
    if api_key is None:
        api_key = os.environ.get("STRATA_API_KEY", DEFAULT_API_KEY)
    return ChatSettings(
        backend=args.backend,
        base_url=args.base_url or os.environ.get("STRATA_BASE_URL") or DEFAULT_BASE_URL,
        model=args.model or os.environ.get("STRATA_MODEL") or DEFAULT_MODEL,
        api_key=api_key,
        timeout=args.timeout,
        max_retries=args.max_retries,
        max_turns=args.max_turns,
        max_context_chars=args.max_context_chars,
        system_prompt=args.system or os.environ.get("STRATA_SYSTEM_PROMPT", ""),
        show_thinking=args.thinking == "show",
        streaming=not args.no_stream,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        request_usage=not args.no_usage,
        session_file=args.session or os.environ.get("STRATA_SESSION_FILE") or None,
    )


def _add_gateway_arguments(sub: argparse.ArgumentParser) -> None:
    group = sub.add_argument_group("模型与网关")
    group.add_argument(
        "--base-url",
        default=None,
        help=f"OpenAI 兼容网关根地址（默认 {DEFAULT_BASE_URL}，可用 STRATA_BASE_URL）",
    )
    group.add_argument(
        "--model", default=None, help=f"模型名（默认 {DEFAULT_MODEL}，可用 STRATA_MODEL）"
    )
    group.add_argument(
        "--api-key",
        default=None,
        help=f"Bearer 令牌；本地网关接受任意串（默认 {DEFAULT_API_KEY}，可用 STRATA_API_KEY）",
    )
    group.add_argument(
        "--backend",
        choices=BACKEND_CHOICES,
        default="strata",
        help="strata=走网关流式；local=离线兜底（默认 strata）",
    )
    group.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="读流超时秒数（默认 %(default)s）",
    )
    group.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help="还没吐字之前的退避重试次数（默认 %(default)s）",
    )
    group.add_argument("--temperature", type=float, default=None, help="透传给网关的采样温度")
    group.add_argument("--max-tokens", type=int, default=None, help="透传给网关的最大生成 token")
    group.add_argument(
        "--no-usage",
        action="store_true",
        help="不请求 usage 帧（默认请求，网关会在最后一帧回 token 与生成速度）",
    )


def _add_memory_arguments(sub: argparse.ArgumentParser) -> None:
    group = sub.add_argument_group("多轮上下文记忆")
    group.add_argument(
        "--system",
        default=None,
        help="系统提示词（默认工程助手人设，可用 STRATA_SYSTEM_PROMPT）",
    )
    group.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help="回灌的最大轮数（默认 %(default)s）",
    )
    group.add_argument(
        "--max-context-chars",
        type=int,
        default=DEFAULT_MAX_CONTEXT_CHARS,
        help="回灌的最大字符预算（默认 %(default)s）",
    )
    group.add_argument(
        "--session",
        default=None,
        help="JSONL 转录文件：启动时恢复历史，每轮追加一行（可用 STRATA_SESSION_FILE）",
    )


def _add_output_arguments(sub: argparse.ArgumentParser) -> None:
    group = sub.add_argument_group("输出与运行方式")
    group.add_argument(
        "--thinking",
        choices=("show", "hide"),
        default="show",
        help="是否显示 🧠 Thinking 区块（默认 show，可运行时 /thinking 切换）",
    )
    group.add_argument(
        "--no-stream", action="store_true", help="整段收完再打印（默认逐 token 流式）"
    )
    group.add_argument(
        "--no-color", action="store_true", help="关闭 ANSI 颜色（管道、重定向、录屏场景）"
    )
    group.add_argument("--prompt", default=None, help="非交互模式：只发这一条消息，打印完就退出")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strata", description="Strata 工程命令行入口")
    parser.add_argument(
        "--version",
        action="store_true",
        help="打印包版本与当前 Python 运行时信息",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="命令")
    chat = subparsers.add_parser("chat", help="进入终端聊天：流式 + 🧠 思维链拦截 + 多轮记忆")
    _add_gateway_arguments(chat)
    _add_memory_arguments(chat)
    _add_output_arguments(chat)
    return parser
