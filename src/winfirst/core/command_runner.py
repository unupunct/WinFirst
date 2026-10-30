"""Central command execution engine.

Design rules
------------
* Argument lists only - no ``shell=True`` and no string concatenation of
  untrusted values.  PowerShell scripts are built by services from validated
  values and passed as an encoded command.
* Executables are resolved and existence-checked before launch.
* Every invocation has an explicit timeout and supports cancellation.
* Output is captured with a size cap and optionally streamed to a progress
  callback.
* Credentials are never added to command lines by this module.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

CREATE_NO_WINDOW = 0x08000000
DEFAULT_TIMEOUT = 90
MAX_OUTPUT_CHARS = 2_000_000

OutputCallback = Callable[[str], None]


class CommandError(RuntimeError):
    """Raised for invalid invocations or unparseable tool output."""


class CommandNotFound(CommandError):
    pass


def _powershell_path() -> str | None:
    if sys.platform == "win32":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        candidate = os.path.join(
            system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
        )
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("powershell.exe") or shutil.which("powershell")


def _creation_flags() -> int:
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass
class CommandResult:
    argv: list[str]
    executable: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    cancelled: bool = False
    launched: bool = False
    error: str = ""
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.launched and not self.timed_out and not self.cancelled and self.exit_code == 0

    @property
    def combined(self) -> str:
        if self.stderr and self.stdout:
            return self.stdout.rstrip() + "\n" + self.stderr
        return self.stdout or self.stderr

    def tail(self, limit: int = 4000) -> str:
        text = self.combined.strip()
        return text if len(text) <= limit else "..." + text[-limit:]


def _validate_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, (str, bytes)):
        raise CommandError("Commands must be passed as an argument list, not a string")
    values = list(argv)
    if not values:
        raise CommandError("Empty command")
    for index, item in enumerate(values):
        if not isinstance(item, str):
            raise CommandError(f"Argument {index} is not a string")
        if "\x00" in item:
            raise CommandError(f"Argument {index} contains a NUL byte")
        if len(item) > 8192:
            raise CommandError(f"Argument {index} is unreasonably long")
    if not values[0].strip():
        raise CommandError("Empty executable name")
    return values


class CommandRunner:
    """Thread-safe command runner with timeouts, cancellation and streaming."""

    def __init__(
        self,
        default_timeout: int = DEFAULT_TIMEOUT,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        logger: object | None = None,
    ) -> None:
        self.default_timeout = default_timeout
        self.max_output_chars = max_output_chars
        self._logger = logger
        self._powershell = _powershell_path()
        self._which_cache: dict[str, str | None] = {}

    # ------------------------------------------------------------------ utils
    @property
    def powershell_available(self) -> bool:
        return bool(self._powershell)

    def which(self, name: str) -> str | None:
        if name in self._which_cache:
            return self._which_cache[name]
        resolved = shutil.which(name)
        self._which_cache[name] = resolved
        return resolved

    def resolve_executable(self, name: str) -> str:
        candidate = name.strip().strip('"')
        if not candidate:
            raise CommandError("Empty executable name")
        has_path = bool(os.path.dirname(candidate))
        if has_path or candidate.lower().endswith((".exe", ".com")):
            expanded = os.path.expandvars(candidate)
            if not os.path.isabs(expanded):
                found = shutil.which(expanded)
                if found:
                    return found
                raise CommandNotFound(f"Executable not found: {expanded}")
            if not os.path.isfile(expanded):
                raise CommandNotFound(f"Executable not found: {expanded}")
            return expanded
        if candidate.lower().endswith((".bat", ".cmd", ".ps1")):
            raise CommandError(
                "Batch and script files are not launched directly by the command runner"
            )
        found = self.which(candidate)
        if not found:
            raise CommandNotFound(f"Executable not found on PATH: {candidate}")
        return found

    def _kill_tree(self, process: subprocess.Popen[str]) -> None:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    creationflags=_creation_flags(),
                )
            else:
                process.terminate()
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            pass

    # -------------------------------------------------------------------- run
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        input_text: str | None = None,
        on_output: OutputCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        started = time.monotonic()
        try:
            values = _validate_argv(argv)
            executable = self.resolve_executable(values[0])
        except (CommandError, OSError) as exc:
            return CommandResult(
                argv=list(argv) if not isinstance(argv, (str, bytes)) else [str(argv)],
                launched=False,
                error=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        full_env = None
        if env is not None:
            full_env = dict(os.environ)
            full_env.update({str(k): str(v) for k, v in env.items()})

        effective_timeout = self.default_timeout if timeout is None else timeout
        result = CommandResult(argv=values, executable=executable, launched=False)
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        lock = threading.Lock()

        def reader(stream, chunks: list[str], emit: bool) -> None:
            try:
                for line in iter(stream.readline, ""):
                    with lock:
                        chunks.append(line)
                    if emit and on_output is not None:
                        try:
                            on_output(line.rstrip("\r\n"))
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        try:
            process = subprocess.Popen(
                values,
                executable=executable,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=cwd,
                env=full_env,
                creationflags=_creation_flags(),
            )
        except (OSError, ValueError) as exc:
            result.error = f"Failed to start {os.path.basename(executable)}: {exc}"
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

        result.launched = True
        threads = [
            threading.Thread(
                target=reader, args=(process.stdout, stdout_chunks, True), daemon=True
            ),
            threading.Thread(
                target=reader, args=(process.stderr, stderr_chunks, False), daemon=True
            ),
        ]
        for thread in threads:
            thread.start()

        if input_text is not None and process.stdin is not None:
            try:
                process.stdin.write(input_text)
                process.stdin.close()
            except OSError:
                pass

        deadline = time.monotonic() + effective_timeout if effective_timeout > 0 else None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True
                self._kill_tree(process)
                break
            if process.poll() is not None:
                break
            if deadline is not None and time.monotonic() >= deadline:
                result.timed_out = True
                self._kill_tree(process)
                break
            time.sleep(0.05)

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._kill_tree(process)
        for thread in threads:
            thread.join(timeout=3)

        with lock:
            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)
        if len(stdout) > self.max_output_chars:
            stdout = stdout[: self.max_output_chars] + "\n... output truncated ..."
            result.truncated = True
        if len(stderr) > self.max_output_chars:
            stderr = stderr[: self.max_output_chars] + "\n... output truncated ..."
            result.truncated = True

        result.stdout = stdout
        result.stderr = stderr
        result.exit_code = process.returncode
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if result.timed_out:
            result.error = f"Command timed out after {int(effective_timeout)} seconds"
        elif result.cancelled:
            result.error = "Command was cancelled"
        return result

    # ------------------------------------------------------------- powershell
    def _wrap_ps(self, script: str) -> str:
        header = (
            "$ErrorActionPreference = 'Stop'\n"
            "$ProgressPreference = 'SilentlyContinue'\n"
            "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }\n"
        )
        return header + script

    def powershell(
        self,
        script: str,
        *,
        timeout: float | None = None,
        as_json: bool = False,
        on_output: OutputCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        if not self._powershell:
            return CommandResult(
                argv=["powershell.exe"], launched=False, error="Windows PowerShell is not available"
            )
        if not isinstance(script, str) or not script.strip():
            raise CommandError("Empty PowerShell script")
        encoded = base64.b64encode(self._wrap_ps(script).encode("utf-16-le")).decode("ascii")
        # -ExecutionPolicy Bypass only affects this one process and only the
        # encoded script WinFirst built itself (no script files are loaded).
        # Machine or group policy (e.g. AppLocker, constrained language mode)
        # still applies and may block commands; that is reported as a failure.
        argv = [
            self._powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]
        result = self.run(
            argv,
            timeout=timeout,
            on_output=on_output,
            cancel_event=cancel_event,
        )
        if as_json and result.ok:
            result.stdout = result.stdout.lstrip("\ufeff")
        return result

    def powershell_json(
        self,
        script: str,
        *,
        timeout: float | None = None,
        default: object = None,
        cancel_event: threading.Event | None = None,
        on_output: OutputCallback | None = None,
    ):
        """Run a script whose stdout is JSON and return the parsed value.

        ``default`` is returned when the command succeeds but produced no
        output (for example an empty pipeline).
        """
        result = self.powershell(
            script, timeout=timeout, cancel_event=cancel_event, on_output=on_output
        )
        if not result.launched:
            raise CommandError(result.error or "PowerShell could not be launched")
        if result.timed_out:
            raise CommandError(result.error)
        if result.exit_code != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise CommandError(f"PowerShell failed (exit {result.exit_code}): {detail[:2000]}")
        text = result.stdout.strip().lstrip("\ufeff")
        if not text:
            return default
        parsed = parse_json_output(text)
        if parsed is _UNPARSEABLE:
            raise CommandError(f"Unexpected non-JSON output: {text[:600]}")
        return parsed

    def powershell_checked(self, script: str, *, timeout: float | None = None) -> CommandResult:
        result = self.powershell(script, timeout=timeout)
        if not result.launched:
            raise CommandError(result.error or "PowerShell could not be launched")
        if result.timed_out:
            raise CommandError(result.error)
        if result.exit_code != 0:
            raise CommandError(
                f"PowerShell failed (exit {result.exit_code}): "
                f"{(result.stderr or result.stdout).strip()[:2000]}"
            )
        return result


_UNPARSEABLE = object()


def parse_json_output(text: str):
    """Parse JSON that may be surrounded by warnings or informational noise."""
    candidate = text.strip()
    if not candidate:
        return _UNPARSEABLE
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    positions = [p for p in (candidate.find("{"), candidate.find("[")) if p >= 0]
    if not positions:
        return _UNPARSEABLE
    # raw_decode parses one value from the first bracket and ignores trailing
    # noise, so large outputs are decoded once instead of once per end offset.
    decoder = json.JSONDecoder()
    index = min(positions)
    while index >= 0:
        try:
            value, _end = decoder.raw_decode(candidate, index)
            return value
        except json.JSONDecodeError:
            pass
        starts = [p for p in (candidate.find("{", index + 1), candidate.find("[", index + 1)) if p >= 0]
        index = min(starts) if starts else -1
    return _UNPARSEABLE


def as_list(value) -> list[dict]:
    """PowerShell emits a bare object for single-item results."""
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def ps_quote(value: str) -> str:
    """Quote a value for a PowerShell single-quoted string.

    Only use for validated values; the command runner never uses a shell, but
    PowerShell itself interprets its script text.
    """
    if not isinstance(value, str):
        value = str(value)
    return "'" + value.replace("'", "''") + "'"
