"""入口层测试：参数解析、环境变量覆盖、装配后的 REPL 行为、色道翻译。

引擎行为（窗口裁剪、SSE 解析、错误语义）在 ``tests/test_chat.py``，这里只盯装配与终端。
"""

from pathlib import Path

import pytest

import strata
import strata.cli as cli


def test_version_flag_prints_runtime(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    out = capsys.readouterr().out
    assert f"strata {strata.__version__}" in out
    assert "python " in out


def test_default_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    assert "usage: strata" in capsys.readouterr().out


def test_unknown_flag_exits_with_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--nope"])
    assert excinfo.value.code == 2


def test_chat_one_shot_offline_backend_prints_both_lanes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["chat", "--backend", "local", "--prompt", "把 D 盘装好", "--no-color"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Strata 聊天 · 模型 iq3_s" in out
    assert "🧠 Thinking ▾" in out
    assert "[local] 已收到" in out


def test_chat_can_hide_the_thinking_lane(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(
        ["chat", "--backend", "local", "--prompt", "hi", "--thinking", "hide", "--no-color"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "[local] 已收到" in out
    assert "🧠 Thinking ▾" not in out  # 拦截区不再输出
    assert "离线兜底后端不联网" not in out  # 思维链正文不外泄
    assert "思维链" in out  # 但仍然留档并计数


def test_chat_rejects_bad_configuration_with_exit_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["chat", "--base-url", "ftp://nope", "--no-color"])
    captured = capsys.readouterr()
    assert code == 2
    assert "ConfigurationError" in captured.err
    assert "http://" in captured.err


def test_chat_repl_walks_the_command_surface(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = iter(
        [
            "/status",
            "/thinking off",
            "你好",
            "/history",
            "/bogus",
            "/retry",
            "/clear",
            "/exit",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(script))
    code = cli.main(["chat", "--backend", "local", "--no-color", "--max-turns", "2"])
    captured = capsys.readouterr()
    assert code == 0
    assert "后端 local" in captured.out
    assert "Thinking 已隐藏" in captured.out
    assert "已清空 1 轮历史" in captured.out
    assert "未知指令 /bogus" in captured.err
    assert "/help 看全部指令" in captured.err


def test_chat_survives_a_dead_gateway_and_keeps_the_repl(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STRATA_BASE_URL", "http://127.0.0.1:1/v1")  # 这个端口没人听
    script = iter(["这条会失败", "/status", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(script))
    code = cli.main(["chat", "--no-color", "--max-retries", "0", "--timeout", "2"])
    captured = capsys.readouterr()
    assert code == 0  # 单轮失败不掀翻 REPL
    assert "http://127.0.0.1:1/v1/chat/completions" in captured.err  # 报的是引擎语义
    assert "↳" in captured.err  # 还给了可操作提示
    assert "后端 gateway" in captured.out  # 失败后还能继续跑 /status


def test_cli_reads_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATA_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("STRATA_MODEL", "env-model")
    monkeypatch.setenv("STRATA_API_KEY", "env-key")
    monkeypatch.setenv("STRATA_SYSTEM_PROMPT", "env 人设")
    settings = cli.settings_from_args(cli.build_parser().parse_args(["chat"]))
    assert settings.base_url == "http://127.0.0.1:1/v1"
    assert settings.model == "env-model"
    assert settings.api_key == "env-key"
    assert settings.system_prompt == "env 人设"


def test_command_line_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATA_MODEL", "env-model")
    args = cli.build_parser().parse_args(["chat", "--model", "cli-model"])
    assert cli.settings_from_args(args).model == "cli-model"


def test_palette_painter_maps_lanes_to_ansi() -> None:
    painter = cli.make_painter(cli.Palette(enabled=True))
    assert painter("思", "thinking") == "\x1b[2m思\x1b[0m"
    assert painter("抬头", "notice") == "\x1b[36m抬头\x1b[0m"
    assert painter("警告", "warn") == "\x1b[33m警告\x1b[0m"
    assert painter("正文", "answer") == "正文"  # 正文不上色，保持可读


def test_palette_disables_itself_for_pipes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli.resolve_palette(no_color=False).enabled is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert cli.resolve_palette(no_color=False).enabled is True
    assert cli.resolve_palette(no_color=True).enabled is False


def test_chat_persists_and_resumes_the_transcript(
    transcript_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = transcript_path
    first = cli.main(
        ["chat", "--backend", "local", "--session", str(log), "--prompt", "第一轮", "--no-color"]
    )
    assert first == 0
    assert log.exists()
    assert "会话转录" in capsys.readouterr().out

    second = cli.main(
        ["chat", "--backend", "local", "--session", str(log), "--prompt", "第二轮", "--no-color"]
    )
    out = capsys.readouterr().out
    assert second == 0
    assert "恢复 1 轮" in out  # banner 说明从转录里捞回了上一进程的历史
    assert "历史 1 轮" in out  # 恢复的轮次确实进了本轮上下文


def test_chat_status_shows_usage_and_transcript(
    transcript_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log = transcript_path
    script = iter(["/status", "/session", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(script))
    code = cli.main(["chat", "--backend", "local", "--session", str(log), "--no-color"])
    captured = capsys.readouterr()
    assert code == 0
    assert "用量 未上报" in captured.out  # 本地后端不回 usage，就照实说
    assert "未启用" not in captured.out  # 转录已开，不该再显示未启用
    assert str(log) in captured.out  # /status 与 /session 都报路径


def test_no_usage_flag_turns_off_usage_frames() -> None:
    parser = cli.build_parser()
    assert cli.settings_from_args(parser.parse_args(["chat"])).request_usage is True
    assert cli.settings_from_args(parser.parse_args(["chat", "--no-usage"])).request_usage is False


def test_session_path_can_come_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATA_SESSION_FILE", "from-env.jsonl")
    settings = cli.settings_from_args(cli.build_parser().parse_args(["chat"]))
    assert settings.session_file == "from-env.jsonl"
