"""The sandbox runner: a child process with a timeout and a memory cap (vault.jobs.sandbox).

The tests start real child processes. On the Linux CI job they exercise the ``RLIMIT_AS`` cap,
the process group kill and ``PR_SET_PDEATHSIG``; on the Windows CI job the Job Object with its
memory limit, ``TerminateJobObject`` and kill-on-close. The limits are small so the tests are fast.
"""

from __future__ import annotations

import importlib
import io
import logging
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vault.core.config import ProcessingSettings, Settings
from vault.jobs import sandbox, sandbox_child, sandbox_probes
from vault.jobs.sandbox import (
    MIB,
    EntryPoint,
    SandboxChildError,
    SandboxCrashError,
    SandboxError,
    SandboxLimits,
    SandboxMemoryLimitError,
    SandboxRefusedError,
    SandboxResultError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)
from vault.jobs.sandbox_probes import ProbeResult

SECRET = "Patient diagnosis confidential: Jane Roe, 1984-02-03"  # noqa: S105 - not a password
PROBES = "vault.jobs.sandbox_probes"
RESULT_TYPE = f"{PROBES}:ProbeResult"
NAMES = (
    "echo",
    "length",
    "describe",
    "fail",
    "fail_with_memory",
    "noisy",
    "allocate",
    "sleep",
    "sleep_with_pid_file",
    "spawn_sleeper",
    "exit_now",
    "abort",
    "unpicklable",
    "large",
    "exit_system",
    "environment_names",
)
ENTRIES = {name: EntryPoint(f"{PROBES}:{name}") for name in NAMES}
ENTRIES["describe_allowed"] = EntryPoint(f"{PROBES}:describe", frozenset({RESULT_TYPE}))
LIMITS = SandboxLimits(timeout_seconds=30, memory_bytes=256 * MIB)
SHORT = SandboxLimits(timeout_seconds=1.5, memory_bytes=256 * MIB)


def answer(target: str, data: bytes) -> bytes:
    """The child's whole answer, as it writes it: the status byte, then the payload."""
    return b"".join(sandbox_child.respond(target, data))


def run(
    name: str, data: bytes | bytearray | memoryview = b"", limits: SandboxLimits = LIMITS
) -> object:
    return sandbox.run(name, data, limits, entry_points=ENTRIES)


def gone(pid: int, within: float = 10.0) -> bool:
    """True once no process has this id (or, on Linux, it is only a zombie nobody waits for)."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes

        synchronize = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.WaitForSingleObject(handle, 0) != 0)  # 0: it has ended
        finally:
            kernel32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def wait_for_file(path: Path, within: float = 15.0) -> int:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.exists() and (text := path.read_text(encoding="ascii")):
            return int(text)
        time.sleep(0.05)
    pytest.fail("the child never wrote its process id")


def assert_quiet(error: BaseException) -> None:
    """Nothing of the secret in the error, its arguments, its chain or its notes."""
    seen = [str(error), repr(error), *map(str, error.args)]
    seen += [repr(getattr(error, attr, None)) for attr in ("__cause__", "__context__")]
    seen += getattr(error, "__notes__", [])
    for text in seen:
        assert "Jane" not in text
        assert "diagnosis" not in text


# --- results --------------------------------------------------------------------------------


def test_returns_the_result() -> None:
    data = bytes(range(256)) * 3 + b"\r\n\x1a\r\x00end"  # newline and end-of-file bytes intact
    assert run("echo", data) == data


def test_an_allowed_result_type_comes_back() -> None:
    assert run("describe_allowed", b"abcdef") == ProbeResult(size=6, head=b"abcd")


def test_a_result_type_not_listed_is_refused_without_being_built() -> None:
    with pytest.raises(SandboxResultError):
        run("describe", b"abcdef")


def test_a_large_input_and_result_cross_both_pipes() -> None:
    data = os.urandom(8 * MIB)  # far more than a pipe buffer, both ways
    assert run("echo", data) == data


def test_an_empty_input() -> None:
    assert run("echo", b"") == b""


def test_a_bytearray_or_memoryview_input() -> None:
    assert run("echo", bytearray(b"abc")) == b"abc"
    assert run("echo", memoryview(b"abc")) == b"abc"


# --- exceptions in the child ----------------------------------------------------------------


def test_an_exception_comes_back_as_its_class_name_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SandboxChildError) as caught:
        run("fail", SECRET.encode())
    assert caught.value.error_class == "ValueError"
    assert_quiet(caught.value)
    assert SECRET not in caplog.text


def test_system_exit_in_the_target_is_an_exception() -> None:
    with pytest.raises(SandboxChildError) as caught:
        run("exit_system", SECRET.encode())
    assert caught.value.error_class == "SystemExit"
    assert_quiet(caught.value)


def test_nothing_the_child_prints_or_logs_reaches_the_console(
    capfd: pytest.CaptureFixture[str],
) -> None:
    assert run("noisy", SECRET.encode()) == len(SECRET)
    out, err = capfd.readouterr()
    assert "Jane" not in out
    assert "Jane" not in err


def test_the_child_gets_no_secret_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    env = sandbox.child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "PATH" in env
    names = run("environment_names")
    assert isinstance(names, list)
    assert "ANTHROPIC_API_KEY" not in names
    assert {name.upper() for name in names} <= sandbox.CHILD_ENV


# --- timeout --------------------------------------------------------------------------------


def test_a_timeout_kills_and_reaps_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)  # type: ignore[call-overload]
        started.append(process)
        return process  # type: ignore[no-any-return]

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    began = time.monotonic()
    with pytest.raises(SandboxTimeoutError) as caught:
        run("sleep", b"60", SHORT)
    assert time.monotonic() - began < 10
    assert_quiet(caught.value)
    (process,) = started
    assert process.returncode is not None  # reaped
    assert gone(process.pid)


def test_a_timeout_also_ends_what_the_child_started(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    with pytest.raises(SandboxTimeoutError):
        run("spawn_sleeper", str(pid_file).encode(), SandboxLimits(5, 256 * MIB))
    assert gone(wait_for_file(pid_file))


def kill_server_after(script: str, pid_file: Path) -> int:
    """Run ``script`` as a stand-in server, wait for the child id it writes, then kill it."""
    server = subprocess.Popen(  # noqa: S603 - a fixed command
        [sys.executable, "-I", "-c", textwrap.dedent(script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        return wait_for_file(pid_file)
    finally:
        server.kill()
        server.wait(10)


def test_a_child_that_dies_with_the_server_leaves_no_orphan(tmp_path: Path) -> None:
    """Linux: PR_SET_PDEATHSIG. Windows: the job's kill-on-close."""
    pid_file = tmp_path / "child.pid"
    script = f"""
        from vault.jobs import sandbox
        entries = {{"s": sandbox.EntryPoint("{PROBES}:sleep_with_pid_file")}}
        limits = sandbox.SandboxLimits(timeout_seconds=60, memory_bytes=256 * sandbox.MIB)
        sandbox.run("s", {str(pid_file).encode()!r}, limits, entry_points=entries)
        """
    assert gone(kill_server_after(script, pid_file))


def test_a_server_killed_before_the_child_is_limited_leaves_no_orphan(tmp_path: Path) -> None:
    """The window between creating the child and putting it in its own job (Windows: the child
    is still suspended; Linux: it has not been sent its input). The stand-in server stops in
    that window, writes the child's id and is killed there."""
    pid_file = tmp_path / "child.pid"
    script = f"""
        import pathlib, time
        from vault.jobs import sandbox

        def stop_here(self, process):
            pathlib.Path({str(pid_file)!r}).write_text(str(process.pid), encoding="ascii")
            time.sleep(60)
            return False

        sandbox._Guard.attach = stop_here
        entries = {{"s": sandbox.EntryPoint("{PROBES}:echo")}}
        limits = sandbox.SandboxLimits(timeout_seconds=60, memory_bytes=256 * sandbox.MIB)
        sandbox.run("s", b"", limits, entry_points=entries)
        """
    assert gone(kill_server_after(script, pid_file))


if sys.platform == "win32":  # pragma: no cover - Job Objects: the Windows CI job only

    def test_the_child_is_in_the_server_job_from_creation(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        process_query_limited_information = 0x1000
        seen: list[bool] = []
        real_attach = sandbox._Guard.attach

        def checking_attach(self: object, process: subprocess.Popen[bytes]) -> bool:
            handle = kernel32.OpenProcess(process_query_limited_information, False, process.pid)
            assert handle
            try:
                inside = wintypes.BOOL()
                assert kernel32.IsProcessInJob(handle, sandbox.server_job(), ctypes.byref(inside))
                seen.append(bool(inside.value))
            finally:
                kernel32.CloseHandle(handle)
            return real_attach(self, process)  # type: ignore[arg-type]

        monkeypatch.setattr(sandbox._Guard, "attach", checking_attach)
        assert run("echo", b"ok") == b"ok"
        assert seen == [True]


# --- memory ---------------------------------------------------------------------------------


def test_an_allocation_past_the_cap_is_a_memory_error() -> None:
    with pytest.raises(SandboxMemoryLimitError):
        run("allocate", str(1024 * MIB).encode())


def test_an_allocation_under_the_cap_succeeds() -> None:
    assert run("allocate", str(64 * MIB).encode()) == 64 * MIB


def test_an_input_is_held_once_while_it_is_read() -> None:
    """128 MiB under a 256 MiB cap: two copies would not fit (with the interpreter beside them)."""
    assert run("length", bytes(128 * MIB)) == 128 * MIB


def test_an_input_that_does_not_fit_while_it_is_read_is_a_memory_error() -> None:
    limits = SandboxLimits(timeout_seconds=30, memory_bytes=128 * MIB)
    with pytest.raises(SandboxMemoryLimitError):
        run("length", bytes(200 * MIB), limits)


def test_a_result_that_does_not_fit_while_it_is_pickled_is_a_memory_error() -> None:
    """The input (128 MiB) fits and the target returns it; its pickled copy does not fit."""
    with pytest.raises(SandboxMemoryLimitError):
        run("echo", bytes(128 * MIB))


def test_running_out_while_importing_the_target_is_a_memory_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exhausted(name: str) -> None:
        raise MemoryError

    monkeypatch.setattr(importlib, "import_module", exhausted)
    assert sandbox_child.respond(f"{PROBES}:echo", b"") == (sandbox_child.MEMORY, b"")


def test_read_input_reads_exactly_the_announced_length(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.write_bytes(b"0123456789")
    with io.FileIO(source) as stream:
        assert sandbox_child.read_input(stream, 10) == b"0123456789"
    with io.FileIO(source) as stream:
        assert sandbox_child.read_input(stream, 11) is None  # shorter than announced
    with io.FileIO(source) as stream:
        assert sandbox_child.read_input(stream, 0) == b""


def test_write_answer_writes_the_status_then_the_payload() -> None:
    reader, writer = os.pipe()
    try:
        sandbox_child.write_answer(writer, sandbox_child.OK, b"payload")
        os.close(writer)
        writer = -1
        assert os.read(reader, 100) == b"Rpayload"
    finally:
        os.close(reader)
        if writer >= 0:
            os.close(writer)


@pytest.mark.parametrize("protocol", [0, 2, 5])
def test_results_of_every_pickle_protocol_are_read_from_a_view(protocol: int) -> None:
    import pickle

    # No bytes: protocols below 3 pickle them through a global (``_codecs.encode``), which the
    # unpickler rightly refuses; the child always uses protocol 5.
    value = [1, "a" * 5000 + "\nline", 2.5, {"k": (None, True)}]
    payload = pickle.dumps(value, protocol=protocol)
    assert sandbox._interpret(sandbox_child.OK + payload, frozenset()) == value
    assert sandbox._interpret(bytearray(sandbox_child.OK + payload), frozenset()) == value


def test_a_memory_error_raised_by_the_target_is_a_memory_error() -> None:
    with pytest.raises(SandboxMemoryLimitError) as caught:
        run("fail_with_memory", SECRET.encode())
    assert_quiet(caught.value)


# --- crashes --------------------------------------------------------------------------------


def test_a_non_zero_exit_is_a_crash() -> None:
    with pytest.raises(SandboxCrashError) as caught:
        run("exit_now", b"7")
    assert caught.value.exit_code == 7


def test_an_exit_without_an_answer_is_a_crash() -> None:
    with pytest.raises(SandboxCrashError) as caught:
        run("exit_now", b"0")
    assert caught.value.exit_code == 0


def test_an_abort_is_a_crash() -> None:
    with pytest.raises(SandboxCrashError) as caught:
        run("abort")
    assert caught.value.exit_code != 0


# --- results that cannot come back ----------------------------------------------------------


def test_a_result_that_cannot_be_pickled() -> None:
    with pytest.raises(SandboxResultError):
        run("unpicklable")


def test_a_result_larger_than_the_cap() -> None:
    limits = SandboxLimits(timeout_seconds=30, memory_bytes=256 * MIB, max_result_bytes=1000)
    with pytest.raises(SandboxResultError):
        run("large", b"5000", limits)
    assert run("large", b"900", limits) == b"x" * 900


@pytest.mark.parametrize(
    "answer",
    [
        sandbox_child.OK + b"not a pickle",
        sandbox_child.MEMORY + b"extra",
        b"Z",
        sandbox_child.UNPICKLABLE,
    ],
)
def test_a_malformed_answer_is_a_result_error(answer: bytes) -> None:
    with pytest.raises(SandboxResultError):
        sandbox._interpret(answer, frozenset())


def test_an_error_name_that_is_not_a_class_name_is_replaced() -> None:
    with pytest.raises(SandboxChildError) as caught:
        sandbox._interpret(sandbox_child.ERROR + b"Jane Roe\n", frozenset())
    assert caught.value.error_class == "Error"


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (sandbox_child.REFUSED, SandboxRefusedError),
        (sandbox_child.SETUP, SandboxUnavailableError),
        (b"", SandboxCrashError),
    ],
)
def test_the_other_answers(answer: bytes, error: type[SandboxError]) -> None:
    with pytest.raises(error):
        sandbox._interpret(answer, frozenset())


def test_the_unpickler_resolves_no_global_name_unlisted() -> None:
    import pickle

    payload = pickle.dumps(os.system)  # what a hijacked child would send to run a command
    with pytest.raises(SandboxResultError):
        sandbox._interpret(sandbox_child.OK + payload, frozenset())


# --- refusals -------------------------------------------------------------------------------


def test_an_entry_not_on_the_allowlist_is_refused() -> None:
    with pytest.raises(SandboxRefusedError):
        run("missing")
    with pytest.raises(SandboxRefusedError):
        sandbox.run("echo", b"", LIMITS)  # the production allowlist has no probes
    with pytest.raises(SandboxRefusedError):
        sandbox.run(1, b"", LIMITS, entry_points=ENTRIES)  # type: ignore[arg-type]


def test_the_production_allowlist_is_empty_until_extraction_is_wired() -> None:
    assert dict(sandbox.ENTRY_POINTS) == {}


@pytest.mark.parametrize(
    "target",
    [
        "os:system",
        "subprocess:run",
        "vault:main",  # a package with no module part
        "vaults.x:f",
        "vault.jobs.sandbox_probes:echo\n",
        "vault.jobs.sandbox_probes:echo ",
        "vault.jobs.sandbox_probes.echo",
        "vault.jobs.sandbox_probes:ProbeResult",  # upper case: a class name
        "vault.jobs.sandbox_probes:echo.__globals__",
        "",
    ],
)
def test_an_entry_point_outside_vault_or_malformed_is_refused(target: str) -> None:
    with pytest.raises(ValueError, match="entry point"):
        EntryPoint(target)
    assert answer(target, b"") == sandbox_child.REFUSED


@pytest.mark.parametrize(
    "target",
    [
        "vault.jobs.sandbox_probes:missing",
        "vault.jobs.sandbox_probes:imported_function",  # a function of another module
        "vault.jobs.sandbox_probes:subprocess",  # a module
        "vault.jobs.no_such_module:echo",
        "vault.jobs.sandbox_probes:logging",
    ],
)
def test_the_child_refuses_what_is_not_a_function_of_that_module(target: str) -> None:
    assert answer(target, b"") == sandbox_child.REFUSED


def test_the_child_refuses_on_its_side_too() -> None:
    entries = {"x": EntryPoint(f"{PROBES}:imported_function")}
    with pytest.raises(SandboxRefusedError):
        sandbox.run("x", b"", LIMITS, entry_points=entries)


@pytest.mark.parametrize("names", [frozenset({"os:system"}), frozenset({"vault.x:Y\n"}), {"a"}])
def test_result_types_must_be_vault_classes(names: frozenset[str]) -> None:
    with pytest.raises(ValueError, match="result types"):
        EntryPoint(f"{PROBES}:echo", names)


def test_the_input_must_be_bytes_and_the_limits_limits() -> None:
    with pytest.raises(TypeError):
        run("echo", "text")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        sandbox.run("echo", b"", object(), entry_points=ENTRIES)  # type: ignore[arg-type]


def test_an_unsupported_system_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "SUPPORTED_PLATFORMS", frozenset())
    with pytest.raises(SandboxUnavailableError):
        run("echo")


def test_a_child_that_cannot_be_limited_is_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attaching failed: the child (on Windows still suspended, outside the job) is killed."""
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)  # type: ignore[call-overload]
        started.append(process)
        return process  # type: ignore[no-any-return]

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    monkeypatch.setattr(sandbox._Guard, "attach", lambda self, process: False)
    with pytest.raises(SandboxUnavailableError):
        run("echo", b"never sent")
    (process,) = started
    assert process.returncode is not None
    assert gone(process.pid)


def test_a_child_that_cannot_start_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError(2, "not found", "/private/path/Jane Roe")

    monkeypatch.setattr(subprocess, "Popen", broken)
    with pytest.raises(SandboxUnavailableError) as caught:
        run("echo")
    assert_quiet(caught.value)


# --- concurrency ----------------------------------------------------------------------------


def test_concurrent_runs_each_get_their_own_result() -> None:
    inputs = [f"input {n}".encode() * (n + 1) for n in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda data: run("echo", data), inputs))
    assert results == inputs


def test_a_timeout_does_not_disturb_runs_beside_it() -> None:
    calls: list[Callable[[], object]] = [
        lambda: run("sleep", b"60", SHORT),
        lambda: run("echo", b"a"),
        lambda: run("allocate", str(1024 * MIB).encode()),
        lambda: run("echo", b"b"),
    ]

    def outcome(call: Callable[[], object]) -> object:
        try:
            return call()
        except SandboxError as error:
            return type(error)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(outcome, calls))
    assert outcomes == [SandboxTimeoutError, b"a", SandboxMemoryLimitError, b"b"]


# --- in-process checks of the child's logic -------------------------------------------------


def test_respond_in_process() -> None:
    assert sandbox_child.respond(f"{PROBES}:echo", b"ab") == (
        sandbox_child.OK,
        __import__("pickle").dumps(b"ab", protocol=5),
    )
    assert answer(f"{PROBES}:fail", SECRET.encode()) == b"EValueError"
    assert answer(f"{PROBES}:fail_with_memory", b"x") == sandbox_child.MEMORY
    assert answer(f"{PROBES}:unpicklable", b"") == sandbox_child.UNPICKLABLE
    assert answer(f"{PROBES}:exit_system", b"x") == b"ESystemExit"
    assert answer(f"{PROBES}:large", b"3")[:1] == sandbox_child.OK
    assert answer(f"{PROBES}:describe", b"abcdef")[:1] == sandbox_child.OK
    assert answer(f"{PROBES}:environment_names", b"")[:1] == sandbox_child.OK


def test_noisy_probe_in_process(capfd: pytest.CaptureFixture[str]) -> None:
    # The probe really writes to both streams, so the child-process test above proves something.
    assert answer(f"{PROBES}:noisy", b"loud")[:1] == sandbox_child.OK
    out, err = capfd.readouterr()
    assert "loud" in out
    assert "loud" in err


def test_error_name_rule() -> None:
    weird = type("Jane Roe", (Exception,), {})
    assert sandbox_child.error_name(weird()) == "Error"
    assert sandbox_child.error_name(KeyError()) == "KeyError"


def test_the_probes_are_named_in_the_test_allowlist() -> None:
    functions = {
        name
        for name, value in vars(sandbox_probes).items()
        if callable(value) and getattr(value, "__module__", None) == PROBES and name.islower()
    }
    assert functions == set(NAMES)


# --- limits and settings --------------------------------------------------------------------


def test_limits_from_settings() -> None:
    settings = Settings.model_validate(
        {"processing": {"sandbox_timeout_seconds": 20, "sandbox_memory_mib": 512}}
    )
    limits = SandboxLimits.from_settings(settings.processing)
    assert limits == SandboxLimits(timeout_seconds=20.0, memory_bytes=512 * MIB)


def test_default_settings() -> None:
    processing = ProcessingSettings()
    assert processing.sandbox_timeout_seconds == 300
    assert processing.sandbox_memory_mib == 2048


def test_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIVAULT_PROCESSING__SANDBOX_TIMEOUT_SECONDS", "45")
    assert Settings().processing.sandbox_timeout_seconds == 45


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("sandbox_timeout_seconds", 0),
        ("sandbox_timeout_seconds", 3601),
        ("sandbox_timeout_seconds", True),
        ("sandbox_timeout_seconds", 1.5),
        ("sandbox_memory_mib", 255),
        ("sandbox_memory_mib", 65537),
        ("sandbox_memory_mib", False),
        ("sandbox_memory_mib", "lots"),
    ],
)
def test_settings_out_of_range_are_refused(key: str, value: object) -> None:
    with pytest.raises(ValueError, match="sandbox"):
        Settings.model_validate({"processing": {key: value}})


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"timeout_seconds": 0, "memory_bytes": 256 * MIB}, ValueError),
        ({"timeout_seconds": float("inf"), "memory_bytes": 256 * MIB}, ValueError),
        ({"timeout_seconds": float("nan"), "memory_bytes": 256 * MIB}, ValueError),
        ({"timeout_seconds": True, "memory_bytes": 256 * MIB}, TypeError),
        ({"timeout_seconds": 1, "memory_bytes": 1 * MIB}, ValueError),
        ({"timeout_seconds": 1, "memory_bytes": 256.0 * MIB}, TypeError),
        ({"timeout_seconds": 1, "memory_bytes": 256 * MIB, "max_result_bytes": 0}, ValueError),
        ({"timeout_seconds": "1", "memory_bytes": 256 * MIB}, TypeError),
    ],
)
def test_limits_are_checked(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        SandboxLimits(**kwargs)  # type: ignore[arg-type]
