"""JSONL 转录：往返、容错、跨进程恢复，以及写不进去时的语义。

临时文件走 ``tests/conftest.py`` 的 ``transcript_path`` 夹具（工作区内自建自删）。
"""

from pathlib import Path

import pytest

import strata.chat as chat
from strata.core import Turn, Usage
from strata.errors import ConversationError


def test_append_then_load_round_trip(transcript_path: Path) -> None:
    chat.append_turn(
        transcript_path, Turn(user="问一", answer="答一", thinking="想一"), now=lambda: 1.0
    )
    chat.append_turn(
        transcript_path,
        Turn(user="问二", answer="答二", thinking="想二", interrupted=True),
        now=lambda: 2.0,
    )
    turns, skipped = chat.load_turns(transcript_path)
    assert skipped == 0
    assert [(turn.user, turn.answer, turn.thinking) for turn in turns] == [
        ("问一", "答一", "想一"),
        ("问二", "答二", "想二"),
    ]
    assert turns[1].interrupted is True
    assert transcript_path.read_text(encoding="utf-8").count("\n") == 2


def test_transcript_keeps_unicode_readable(transcript_path: Path) -> None:
    chat.append_turn(
        transcript_path, Turn(user="中文 中文", answer="答", thinking=""), now=lambda: 0.0
    )
    raw = transcript_path.read_text(encoding="utf-8")
    assert "中文 中文" in raw  # 不转义成 \uXXXX，人可以直接 grep
    assert '"v": 1' in raw


def test_broken_lines_are_skipped_not_fatal(transcript_path: Path) -> None:
    transcript_path.write_text(
        "\n".join(
            (
                '{"v": 1, "user": "好行", "answer": "答", "thinking": "", "interrupted": false}',
                "{ 这不是 JSON",
                '{"v": 99, "user": "版本不认识", "answer": "x"}',
                '{"v": 1, "user": 42, "answer": "字段类型不对"}',
                "",
            )
        ),
        encoding="utf-8",
    )
    turns, skipped = chat.load_turns(transcript_path)
    assert [turn.user for turn in turns] == ["好行"]
    assert skipped == 3


def test_missing_file_is_an_empty_transcript(transcript_path: Path) -> None:
    turns, skipped = chat.load_turns(transcript_path)
    assert turns == []
    assert skipped == 0


def test_append_creates_missing_parent_directories(transcript_path: Path) -> None:
    """父目录不存在也要能落盘；草稿目录整体由会话级夹具回收。"""
    nested = transcript_path.parent / "嵌套" / "更深" / transcript_path.name
    chat.append_turn(nested, Turn(user="u", answer="a"), now=lambda: 0.0)
    assert nested.exists()
    turns, skipped = chat.load_turns(nested)
    assert skipped == 0
    assert [turn.user for turn in turns] == ["u"]


def test_unwritable_transcript_reports_conversation_error(transcript_path: Path) -> None:
    blocker = transcript_path.parent / "不是目录"
    blocker.write_text("占位", encoding="utf-8")
    try:
        with pytest.raises(ConversationError) as excinfo:
            chat.append_turn(blocker / "session.jsonl", Turn(user="u", answer="a"))
        assert "写不进转录" in excinfo.value.message
        assert "session.jsonl" in str(excinfo.value.context["path"])
    finally:
        blocker.unlink(missing_ok=True)


def test_transcript_log_restores_then_appends(transcript_path: Path) -> None:
    first = chat.TranscriptLog(transcript_path)
    assert first.turns == []
    first.append(Turn(user="第一轮", answer="A1", thinking="T1"))
    first.append(Turn(user="第二轮", answer="A2"))
    assert first.saved == 2
    assert "恢复 0 轮" in first.describe()
    assert "已写 2 行" in first.describe()

    reopened = chat.TranscriptLog(transcript_path)
    assert [turn.user for turn in reopened.turns] == ["第一轮", "第二轮"]
    assert "恢复 2 轮" in reopened.describe()


def test_usage_travels_with_the_transcript(transcript_path: Path) -> None:
    """账要跟着转录走：换进程恢复后 ``/status`` 不会归零。"""
    usage = Usage(
        input_tokens=55,
        output_tokens=16,
        total_tokens=71,
        cached_input_tokens=4,
        output_per_second=17.7,
    )
    chat.append_turn(transcript_path, Turn(user="u1", answer="a1"), usage=usage, now=lambda: 1.0)
    chat.append_turn(transcript_path, Turn(user="u2", answer="a2"), now=lambda: 2.0)

    entries, skipped = chat.load_entries(transcript_path)
    assert skipped == 0
    assert entries[0].usage == usage
    assert entries[1].usage is None  # 网关没报账的那轮就不写这个键

    log = chat.TranscriptLog(transcript_path)
    assert len(log.turns) == 2
    assert log.usages == [usage]
    assert "带账 1 轮" in log.describe()


def test_restored_transcript_feeds_the_next_request(transcript_path: Path) -> None:
    """跨进程续聊的引擎侧证据：恢复出来的轮次会进下一轮上下文。"""
    chat.append_turn(
        transcript_path,
        Turn(user="我最喜欢的数字是 7", answer="收到", thinking="想"),
        now=lambda: 1.0,
    )
    conversation = chat.Conversation("SYS", max_turns=5, max_context_chars=4000)
    turns, _skipped = chat.load_turns(transcript_path)
    assert conversation.load(turns) == 1
    messages = conversation.messages_for("我说过什么数字？")
    joined = " ".join(message.content for message in messages)
    assert "我最喜欢的数字是 7" in joined
    assert "收到" in joined
    assert "想" not in joined  # 恢复出来的思维链同样不回灌


def test_load_respects_the_turn_window(transcript_path: Path) -> None:
    for index in range(6):
        chat.append_turn(
            transcript_path, Turn(user=f"u{index}", answer=f"a{index}"), now=lambda: 0.0
        )
    conversation = chat.Conversation("S", max_turns=3, max_context_chars=4000)
    turns, _skipped = chat.load_turns(transcript_path)
    assert conversation.load(turns) == 3
    assert [turn.user for turn in conversation.turns] == ["u3", "u4", "u5"]
