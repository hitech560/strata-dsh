"""🧠 Thinking 思维链拦截：把同一条流切成两条道。

引擎侧只决定“哪段文本属于哪个语义色道（:data:`~strata.core.Lane`）”，怎么上色、
写到哪个流由装配层注入（``painter`` / ``write`` / ``flush``），所以这一段在没有
终端的地方也能跑（门禁、测试、重定向）。
"""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from strata.core import Lane, LanePainter, StreamEvent, StreamResult, ThinkingDelta

THINKING_HEADER = "🧠 Thinking ▾ "
THINKING_END = "🧠 思维链结束"
ANSWER_HEADER = "🤖 回答 ▸ "

__all__ = [
    "ANSWER_HEADER",
    "THINKING_END",
    "THINKING_HEADER",
    "ThinkingInterceptor",
]


@dataclass(slots=True, kw_only=True)
class ThinkingInterceptor:
    """把 :data:`~strata.core.StreamEvent` 流拦截成分区输出。

    思维链与正文分区输出、分区留档，只有正文会进入下一轮上下文；``streaming=False``
    时先收全再一次性吐出；Ctrl+C 中断会把已产出的内容收进结果，不丢半截。
    """

    painter: LanePainter
    write: Callable[[str], None]
    flush: Callable[[], None] = lambda: None
    show_thinking: bool = True
    streaming: bool = True
    clock: Callable[[], float] = time.monotonic

    _buffer: list[str] = field(default_factory=list, init=False, repr=False)
    _thinking: list[str] = field(default_factory=list, init=False, repr=False)
    _answer: list[str] = field(default_factory=list, init=False, repr=False)
    _phase: str = field(default="idle", init=False, repr=False)

    def consume(self, events: Iterable[StreamEvent]) -> StreamResult:
        started = self.clock()
        interrupted = False
        try:
            for event in events:
                if isinstance(event, ThinkingDelta):
                    self._on_thinking(event.text)
                else:
                    self._on_answer(event.text)
        except KeyboardInterrupt:
            interrupted = True
            self._emit("\n⏸ 已中断，保留已产出的内容\n", "warn")
        self._finish(self.clock() - started)
        return StreamResult(
            answer="".join(self._answer),
            thinking="".join(self._thinking),
            elapsed=self.clock() - started,
            interrupted=interrupted,
        )

    def _on_thinking(self, text: str) -> None:
        if not text:
            return
        self._thinking.append(text)
        if self._phase == "idle":
            self._phase = "thinking"
            if self.show_thinking:
                self._emit(f"\n{THINKING_HEADER}", "muted")
        if self.show_thinking:
            self._emit(text, "thinking")

    def _on_answer(self, text: str) -> None:
        if not text:
            return
        self._answer.append(text)
        if self._phase != "answer":
            if self._phase == "thinking" and self.show_thinking:
                self._emit(f"\n{THINKING_END}\n", "muted")
            self._phase = "answer"
            self._emit(ANSWER_HEADER, "notice")
        self._emit(text, "answer")

    def _finish(self, elapsed: float) -> None:
        if self._phase == "thinking" and self.show_thinking:
            self._emit(f"\n{THINKING_END}\n", "muted")
        answer = "".join(self._answer)
        thinking = "".join(self._thinking)
        if not answer and not thinking:
            self._emit("（模型没有任何输出）\n", "warn")
        elif not answer:
            self._emit("\n（本轮只有思维链，没有正文：生成预算可能用完或被上游截断）\n", "warn")
        else:
            self._emit("\n", "answer")
        sizes = f"正文 {len(answer)} 字 · 思维链 {len(thinking)} 字"
        self._emit(f"· {sizes} · 耗时 {elapsed:.1f}s\n", "muted")
        if not self.streaming:
            self.write("".join(self._buffer))
            self._buffer.clear()
        self.flush()

    def _emit(self, text: str, lane: Lane) -> None:
        styled = self.painter(text, lane)
        if self.streaming:
            self.write(styled)
            self.flush()
            return
        self._buffer.append(styled)
