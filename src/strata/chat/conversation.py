"""多轮对话的上下文记忆：滚动窗口的裁剪规则都在这一个文件里。"""

from strata.core import ChatMessage, Turn, clip
from strata.errors import ConfigurationError, ContextOverflowError, NothingToRetryError

DEFAULT_SYSTEM_PROMPT = (
    "你是 Strata，跑在终端里的工程助手。回答直接、可落地，默认中文；"
    "涉及代码时给可粘贴的完整片段，不写无关注释。"
)
DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_CONTEXT_CHARS = 12_000

__all__ = [
    "DEFAULT_MAX_CONTEXT_CHARS",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_SYSTEM_PROMPT",
    "Conversation",
    "validate_window",
]


def validate_window(*, max_turns: int, max_context_chars: int) -> None:
    """窗口上限的单一出处：配置校验与 :class:`Conversation` 都走这里。"""
    if max_turns < 1:
        raise ConfigurationError("max_turns 至少为 1", context={"max_turns": max_turns})
    if max_context_chars < 64:
        raise ConfigurationError(
            "max_context_chars 至少为 64",
            context={"max_context_chars": max_context_chars},
        )


class Conversation:
    """多轮对话的上下文记忆。

    ``max_turns`` 与 ``max_context_chars`` 是双重上限，谁先到按谁裁剪，且永远保留最近一轮。
    思维链存进 :class:`~strata.core.Turn` 供 ``/history`` 查看，但不回灌：推理类网关的
    ``reasoning_content`` 只出不进，回灌既贵又会被模型当成正文续写（见
    ``docs/gateway-stream-protocol.md``）。
    """

    def __init__(
        self,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    ) -> None:
        validate_window(max_turns=max_turns, max_context_chars=max_context_chars)
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.max_context_chars = max_context_chars
        self._turns: list[Turn] = []

    @property
    def turns(self) -> tuple[Turn, ...]:
        return tuple(self._turns)

    def add_turn(
        self,
        user: str,
        answer: str,
        thinking: str = "",
        *,
        interrupted: bool = False,
    ) -> Turn:
        turn = Turn(user=user, answer=answer, thinking=thinking, interrupted=interrupted)
        self._turns.append(turn)
        overflow = len(self._turns) - self.max_turns
        if overflow > 0:
            del self._turns[:overflow]
        return turn

    def drop_last(self) -> Turn:
        if not self._turns:
            raise NothingToRetryError("还没有可重放的对话轮次")
        return self._turns.pop()

    def clear(self) -> int:
        removed = len(self._turns)
        self._turns.clear()
        return removed

    def messages_for(self, user_text: str) -> list[ChatMessage]:
        """本轮请求的上下文：system + 装得下的历史轮次 + 本轮输入（新的在最后）。

        正文为空的轮次（例如思维链把生成预算用尽）只回灌用户那侧，不往上下文里塞一条
        空的 assistant 消息——部分网关会直接拒收。
        """
        budget = self.max_context_chars - len(self.system_prompt)
        if len(user_text) > budget:
            raise ContextOverflowError(
                "本轮输入超出上下文预算，请精简后重发",
                limit=budget,
                size=len(user_text),
                context={"max_context_chars": self.max_context_chars},
            )
        used = len(user_text)
        kept: list[ChatMessage] = []
        for turn in reversed(self._turns):
            cost = len(turn.user) + len(turn.answer)
            if used + cost > budget:
                break
            used += cost
            if turn.answer.strip():
                kept.insert(0, ChatMessage(role="assistant", content=turn.answer))
            kept.insert(0, ChatMessage(role="user", content=turn.user))
        return [
            ChatMessage(role="system", content=self.system_prompt),
            *kept,
            ChatMessage(role="user", content=user_text),
        ]

    def window_chars(self, user_text: str = "") -> int:
        """当前窗口实际占用字符数（含本轮输入，不含 system）。"""
        used = len(user_text)
        for turn in reversed(self._turns):
            cost = len(turn.user) + len(turn.answer)
            if used + cost > self.max_context_chars:
                break
            used += cost
        return used

    def render(self, *, include_thinking: bool = False, limit: int = 120) -> str:
        """给人看的历史列表；默认只列正文，思维链要显式要。"""
        if not self._turns:
            return "（当前没有历史轮次）"
        lines: list[str] = []
        for index, turn in enumerate(self._turns, start=1):
            marker = " ·已中断" if turn.interrupted else ""
            lines.append(f"[{index}/{self.max_turns}] 你 ▸ {clip(turn.user, limit)}{marker}")
            if include_thinking and turn.thinking:
                lines.append(f"            🧠 {clip(turn.thinking, limit)}")
            lines.append(f"            🤖 {clip(turn.answer, limit)}")
        return "\n".join(lines)
