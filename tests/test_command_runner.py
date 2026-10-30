from __future__ import annotations

import pytest

from winfirst.core import command_runner
from winfirst.core.command_runner import (
    CommandError,
    CommandResult,
    _validate_argv,
    as_list,
    parse_json_output,
    ps_quote,
)


def test_argument_lists_are_required() -> None:
    with pytest.raises(CommandError, match="argument list"):
        _validate_argv("not a list")
    with pytest.raises(CommandError, match="Empty command"):
        _validate_argv([])
    with pytest.raises(CommandError, match="not a string"):
        _validate_argv(["cmd", 3])
    with pytest.raises(CommandError, match="NUL"):
        _validate_argv(["cmd", "a\x00b"])
    with pytest.raises(CommandError, match="unreasonably long"):
        _validate_argv(["cmd", "a" * 9000])
    with pytest.raises(CommandError, match="Empty executable"):
        _validate_argv(["   "])


def test_valid_argument_list_is_returned() -> None:
    assert _validate_argv(["cmd.exe", "/c", "echo"]) == ["cmd.exe", "/c", "echo"]


def test_ps_quote_escapes_single_quotes() -> None:
    assert ps_quote("plain") == "'plain'"
    assert ps_quote("it's") == "'it''s'"


def test_as_list_handles_single_and_arrays() -> None:
    assert as_list(None) == []
    assert as_list({"name": "one"}) == [{"name": "one"}]
    assert as_list([{"a": 1}, "not a dict", 3]) == [{"a": 1}]
    assert as_list("string") == []


def test_parse_json_output_handles_noise_and_bom() -> None:
    assert parse_json_output('{"a": 1}') == {"a": 1}
    assert parse_json_output('warning text\n[{"a": 1}]\nmore') == [{"a": 1}]
    assert parse_json_output("\ufeff{\"a\": 2}") == {"a": 2}
    assert parse_json_output("") is command_runner._UNPARSEABLE
    assert parse_json_output("no json here") is command_runner._UNPARSEABLE


def test_command_result_flags_and_tail() -> None:
    result = CommandResult(
        argv=["cmd"],
        exit_code=0,
        stdout="ok",
        stderr="",
        launched=True,
    )
    assert result.ok is True
    assert result.combined == "ok"
    assert result.tail(2) == "ok"

    failed = CommandResult(argv=["cmd"], exit_code=1, stdout="x", stderr="bad", launched=True)
    assert failed.ok is False
    assert "bad" in failed.combined
    assert failed.tail(2) == "...ad"


def test_runner_exposes_powershell_availability_without_running_it() -> None:
    runner = command_runner.CommandRunner()
    assert isinstance(runner.powershell_available, bool)


def test_run_rejects_string_commands_at_runtime() -> None:
    runner = command_runner.CommandRunner()
    result = runner.run("powershell -Command Get-Date", timeout=1)  # type: ignore[arg-type]
    assert result.launched is False
    assert "argument list" in result.error


def test_unknown_executable_is_reported() -> None:
    runner = command_runner.CommandRunner()
    result = runner.run(["definitely-not-a-real-executable-42.exe"], timeout=2)
    assert result.launched is False
    assert result.error


def test_parse_json_output_ignores_surrounding_noise():
    from winfirst.core.command_runner import parse_json_output

    assert parse_json_output('WARNING: x [y]\n{"a": [1, 2]}\ntrailing') == {"a": [1, 2]}
    assert parse_json_output("[1, 2] done") == [1, 2]


def test_parse_json_output_large_noisy_output_is_fast():
    import json
    import time

    from winfirst.core.command_runner import parse_json_output

    payload = json.dumps([{"n": i, "s": "x" * 20} for i in range(20000)])
    started = time.monotonic()
    assert len(parse_json_output("noise {not json}\n" + payload + "\ntail")) == 20000
    assert time.monotonic() - started < 2
