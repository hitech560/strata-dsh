"""JSONL 转录：一轮一行，重启进程后能接着聊，token 账目也一起接着。

格式（每行一个 JSON 对象，UTF-8，不转义中文）::

    {
        "v": 1,
        "ts": 1794186487.0,
        "user": "…",
        "answer": "…",
        "thinking": "…",
        "interrupted": false,
        "usage": {
            "prompt_tokens": 55,
            "completion_tokens": 16,
            "total_tokens": 71,
            "prompt_tokens_details": {"cached_tokens": 0},
            "predicted_per_second": 17.7,
        },
    }

``usage`` 是可省的：网关没回 usage 的那轮就不写这个键。

读的时候**容错**：空行、坏 JSON、版本不认识、字段类型不对一律跳过并计数，
绝不因为一行烂了就整份打不开。思维链照样只留档不回灌
（恢复出来的 :class:`~strata.core.Turn` 走同一条规则）。
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from strata.core import Turn, Usage
from strata.errors import ConversationError

TRANSCRIPT_VERSION = 1

__all__ = [
    "TRANSCRIPT_VERSION",
    "Entry",
    "TranscriptLog",
    "append_turn",
    "load_entries",
    "load_turns",
    "turn_writer",
]


@dataclass(frozen=True, slots=True)
class Entry:
    """转录里的一条记录：一轮问答，外加该轮的用量（如果网关报过）。"""

    turn: Turn
    usage: Usage | None = None


class TranscriptLog:
    """一份 JSONL 转录：打开时就地恢复历史与用量账，之后每轮追加一行。

    装配层用法：``log = TranscriptLog(path)`` → ``conversation.load(log.turns)`` 恢复上下文、
    把 ``log.append`` 当会话的 ``on_turn`` sink、用 ``log.usages`` 续上 token 账。
    """

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = path
        self._now = now
        entries, skipped = load_entries(path)
        self.entries = entries
        self.turns = [entry.turn for entry in entries]
        self.usages = [entry.usage for entry in entries if entry.usage is not None]
        self.skipped = skipped
        self.saved = 0

    def append(self, turn: Turn, usage: Usage | None = None) -> None:
        append_turn(self.path, turn, usage=usage, now=self._now)
        self.saved += 1

    def describe(self) -> str:
        """一行状态，banner 与 ``/status`` 共用（不含任何终端样式）。"""
        parts = [f"恢复 {len(self.turns)} 轮"]
        if self.skipped:
            parts.append(f"跳过坏行 {self.skipped}")
        if self.usages:
            parts.append(f"带账 {len(self.usages)} 轮")
        parts.append(f"已写 {self.saved} 行")
        return f"{self.path} · " + " · ".join(parts)


def load_turns(path: Path) -> tuple[list[Turn], int]:
    """读转录的轮次；返回 ``(可用的轮次, 被跳过的行数)``。文件不存在算空转录。"""
    entries, skipped = load_entries(path)
    return [entry.turn for entry in entries], skipped


def load_entries(path: Path) -> tuple[list[Entry], int]:
    """读转录的完整条目（含用量）；返回 ``(条目, 被跳过的行数)``。"""
    if not path.exists():
        return [], 0
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConversationError(
            "读不了转录文件", context={"path": str(path), "reason": str(exc)}
        ) from exc
    entries: list[Entry] = []
    skipped = 0
    for line in raw.splitlines():
        if not line.strip():
            continue  # 空行不算坏行，也不计数
        entry = _parse_line(line)
        if entry is None:
            skipped += 1
            continue
        entries.append(entry)
    return entries, skipped


def _parse_line(line: str) -> Entry | None:
    """一行 → :class:`Entry`；不认的版本或结构不对就返回 ``None``（由调用方计数）。"""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("v") != TRANSCRIPT_VERSION:
        return None
    user = record.get("user")
    answer = record.get("answer")
    if not isinstance(user, str) or not isinstance(answer, str):
        return None
    thinking = record.get("thinking")
    turn = Turn(
        user=user,
        answer=answer,
        thinking=thinking if isinstance(thinking, str) else "",
        interrupted=bool(record.get("interrupted")),
    )
    return Entry(turn=turn, usage=_usage_from_record(record.get("usage")))


def _usage_from_record(raw: object) -> Usage | None:
    """把记录里的 ``usage`` 还原成 :class:`~strata.core.Usage`；缺字段就当没报过。"""
    if not isinstance(raw, dict):
        return None
    details = raw.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    speed = raw.get("predicted_per_second")
    return Usage(
        input_tokens=_as_int(raw.get("prompt_tokens")),
        output_tokens=_as_int(raw.get("completion_tokens")),
        total_tokens=_as_int(raw.get("total_tokens")),
        cached_input_tokens=_as_int(cached),
        output_per_second=_as_float(speed),
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def append_turn(
    path: Path,
    turn: Turn,
    *,
    usage: Usage | None = None,
    now: Callable[[], float] = time.time,
    flush: bool = True,
) -> None:
    """追加一行转录（含该轮 usage，若有）。父目录不存在就建。

    写失败抛 :class:`~strata.errors.ConversationError`：账不能默默丢。
    """
    record: dict[str, object] = {
        "v": TRANSCRIPT_VERSION,
        "ts": now(),
        "user": turn.user,
        "answer": turn.answer,
        "thinking": turn.thinking,
        "interrupted": turn.interrupted,
    }
    if usage is not None:
        record["usage"] = {
            "prompt_tokens": usage.input_tokens,
            "completion_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "prompt_tokens_details": {"cached_tokens": usage.cached_input_tokens},
            "predicted_per_second": usage.output_per_second,
        }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            if flush:
                handle.flush()
    except OSError as exc:
        raise ConversationError(
            "写不进转录文件", context={"path": str(path), "reason": str(exc)}
        ) from exc


def turn_writer(
    path: Path, *, now: Callable[[], float] = time.time
) -> Callable[[Turn, Usage | None], None]:
    """给会话用的落盘 sink：``ChatSession(on_turn=turn_writer(path))``。"""

    def write(turn: Turn, usage: Usage | None = None) -> None:
        append_turn(path, turn, usage=usage, now=now)

    return write
