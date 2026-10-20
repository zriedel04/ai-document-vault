"""The sandbox runner: a child process with a timeout and a memory cap (vault.jobs.sandbox).

The tests start real child processes. On the Linux CI job they exercise the ``RLIMIT_AS`` cap,
the process group kill and ``PR_SET_PDEATHSIG``; on the Windows CI job the Job Object with its
memory limit, ``TerminateJobObject`` and kill-on-close. The limits are small so the tests are fast.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import logging
import math
import os
import pickle
import subprocess
import sys
import textwrap
import time
import tracemalloc
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import InitVar, dataclass, field
from enum import IntFlag
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Optional, Union

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
from vault.jobs.sandbox_probes import ProbeKinds, ProbePage, ProbeResult

SECRET = "Patient diagnosis confidential: Jane Roe, 1984-02-03"  # noqa: S105 - not a password
PROBES = "vault.jobs.sandbox_probes"
# Each probe and the type it returns.
RESULT_TYPES: dict[str, object] = {
    "echo": bytes,
    "length": int,
    "describe": ProbeResult,
    "fail": None,
    "fail_with_memory": None,
    "noisy": int,
    "allocate": int,
    "sleep": None,
    "sleep_with_pid_file": None,
    "spawn_sleeper": None,
    "exit_now": None,
    "abort": None,
    "unencodable": None,
    "large": bytes,
    "many_lists": list[list[int]],
    "exit_system": None,
    "environment_names": list[str],
    "every_kind": ProbeKinds,
    "start_processes": list[str],
    "start_thread": int,
}
NAMES = tuple(RESULT_TYPES)
ENTRIES = {name: EntryPoint(f"{PROBES}:{name}", kind) for name, kind in RESULT_TYPES.items()}
# The same function declared with another type: its answer does not fit and is refused.
ENTRIES["describe_as_bytes"] = EntryPoint(f"{PROBES}:describe", bytes)
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


def test_a_declared_dataclass_comes_back() -> None:
    assert run("describe", b"abcdef") == ProbeResult(size=6, head=b"abcd")


def test_a_result_of_another_type_than_declared_is_refused() -> None:
    with pytest.raises(SandboxResultError):
        run("describe_as_bytes", b"abcdef")


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


@pytest.mark.skipif(sys.platform != "win32", reason="Linux: the child cannot start a process")
def test_a_timeout_also_ends_what_the_child_started(tmp_path: Path) -> None:  # pragma: no cover
    """Windows: the job holds what the child starts, under the same cap, and ends it."""
    pid_file = tmp_path / "grandchild.pid"
    with pytest.raises(SandboxTimeoutError):
        run("spawn_sleeper", str(pid_file).encode(), SandboxLimits(5, 256 * MIB))
    assert gone(wait_for_file(pid_file))


@pytest.mark.skipif(sys.platform != "linux", reason="the seccomp filter is Linux only")
def test_the_child_cannot_start_a_process_on_linux(tmp_path: Path) -> None:
    """``RLIMIT_AS`` caps each process, so a second process would have a cap of its own: the
    filter refuses every way of starting one, and a target that tries fails."""
    assert run("start_processes") == []
    with pytest.raises(SandboxChildError) as caught:
        run("spawn_sleeper", str(tmp_path / "never.pid").encode())
    assert caught.value.error_class == "PermissionError"
    assert not (tmp_path / "never.pid").exists()


def test_a_process_can_be_started_without_the_filter() -> None:
    """The control: in this process, without the filter, the probe does start processes, so the
    empty list above is the filter's doing."""
    if sys.platform != "linux":  # pragma: no cover - os.fork is POSIX
        pytest.skip("the probe forks")
    assert "subprocess" in sandbox_probes.start_processes(b"")


def test_threads_still_start_in_the_child() -> None:
    """glibc starts a thread with ``clone3`` and, on ``ENOSYS``, with ``clone``."""
    assert run("start_thread", b"abcd") == 4


def run_filter(program: list[tuple[int, int, int, int]], arch: int, nr: int, flags: int) -> int:
    """An interpreter for the instructions the filter uses, over one ``seccomp_data``."""
    data = {0: nr, 4: arch, 16: flags & 0xFFFFFFFF}
    accumulator, pc = 0, 0
    while True:
        code, jt, jf, k = program[pc]
        if code == 0x20:
            accumulator = data[k]
            pc += 1
        elif code == 0x06:
            return k
        else:
            taken = {0x15: accumulator == k, 0x35: accumulator >= k, 0x45: accumulator & k != 0}
            pc += 1 + (jt if taken[code] else jf)


ALLOW, EPERM, ENOSYS = 0x7FFF0000, 0x00050001, 0x00050026
THREAD = 0x003D0F00  # the flags glibc's pthread_create passes, CLONE_THREAD among them
FILTER_CASES = {
    "x86_64": [
        ((0xC000003E, 0, 0), ALLOW),  # read
        ((0xC000003E, 59, 0), ALLOW),  # execve: replaces this process, under its cap
        ((0xC000003E, 57, 0), EPERM),  # fork
        ((0xC000003E, 58, 0), EPERM),  # vfork
        ((0xC000003E, 56, 0x11), EPERM),  # clone as fork does it (SIGCHLD)
        ((0xC000003E, 56, 0x4111), EPERM),  # clone as posix_spawn does it (CLONE_VM|VFORK)
        ((0xC000003E, 56, THREAD), ALLOW),
        ((0xC000003E, 435, 0), ENOSYS),  # clone3
        ((0xC000003E, 0x40000000 | 57, 0), EPERM),  # x32
        ((0x40000003, 2, 0), EPERM),  # i386: fork is 2
        ((0x40000003, 0, 0), EPERM),
    ],
    "aarch64": [
        ((0xC00000B7, 63, 0), ALLOW),  # read
        ((0xC00000B7, 220, 0x11), EPERM),
        ((0xC00000B7, 220, THREAD), ALLOW),
        ((0xC00000B7, 435, 0), ENOSYS),
        ((0x40000028, 2, 0), EPERM),  # arm: fork is 2
    ],
}


@pytest.mark.parametrize("machine", FILTER_CASES)
def test_the_filter_refuses_every_way_to_start_a_process(machine: str) -> None:
    program = sandbox_child.no_new_process_filter(machine)
    assert program is not None
    for (arch, nr, flags), action in FILTER_CASES[machine]:
        assert run_filter(program, arch, nr, flags) == action, (arch, nr, flags)


def test_an_unknown_architecture_has_no_filter() -> None:
    """The child then reports that it cannot limit itself: SandboxUnavailableError."""
    assert sandbox_child.no_new_process_filter("riscv64") is None
    assert sandbox_child.no_new_process_filter("i686") is None


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
        entries = {{"s": sandbox.EntryPoint("{PROBES}:sleep_with_pid_file", None)}}
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
        entries = {{"s": sandbox.EntryPoint("{PROBES}:echo", bytes)}}
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


def test_a_result_that_does_not_fit_while_it_is_encoded_is_a_memory_error() -> None:
    """The input (128 MiB) fits and the target returns it; its encoded copies do not fit."""
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


def test_a_result_that_cannot_be_encoded() -> None:
    with pytest.raises(SandboxResultError):
        run("unencodable")


def test_a_result_larger_than_the_cap() -> None:
    limits = SandboxLimits(timeout_seconds=30, memory_bytes=256 * MIB, max_result_bytes=2000)
    with pytest.raises(SandboxResultError):
        run("large", b"5000", limits)
    assert run("large", b"900", limits) == b"x" * 900


# --- the answer: JSON built into the declared type ----------------------------------------


class ParserRanError(BaseException):
    """Not an ``Exception``, so ``_load_result`` cannot turn it into a refusal."""


def never(*args: object, **kwargs: object) -> object:
    raise ParserRanError


def decoder(result_type: object) -> sandbox._Node:
    return sandbox._compile_result_type(result_type)


def decode(payload: bytes, result_type: object, **limits: int) -> object:
    """The server's reading of an answer whose payload is ``payload``."""
    return sandbox._interpret(sandbox_child.OK + payload, decoder(result_type), **limits)


def round_trip(value: object, result_type: object) -> object:
    return decode(sandbox_child.encode(value), result_type)


@dataclass(frozen=True)
class Empty:
    pass


@dataclass
class One:
    a: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Slotted:
    name: str
    pages: tuple[int, ...]


@dataclass(frozen=True)
class Tree:
    label: str
    children: list[Tree]


@dataclass
class Checked:
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            msg = "negative"
            raise ValueError(msg)


@dataclass
class WithDict:
    names: dict[str, int]


@dataclass
class NotInitialised:
    a: int
    b: int = field(init=False, default=0)


@dataclass
class Unresolvable:
    a: Missing  # type: ignore[name-defined]  # noqa: F821 - the point of the test


class Plain:
    a: int


# Dataclass shapes the converter could not rebuild exactly: refused at definition.
@dataclass
class WithInitVar:
    a: int
    scale: InitVar[int]


@dataclass
class WithDefaultInitVar:
    a: int
    scale: InitVar[int] = 1


@dataclass
class OwnInit:
    a: int

    def __init__(self, a: int) -> None:
        self.a = a * 2


class InitBase:
    def __init__(self, a: int) -> None:
        self.a = a + 1


@dataclass(init=False)
class InheritedInit(InitBase):
    a: int


@dataclass
class PostInitChanges:
    text: str

    def __post_init__(self) -> None:
        self.text = self.text.strip()


@dataclass
class OwnSetattr:
    a: int

    def __setattr__(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)


@dataclass
class OwnNew:
    a: int

    def __new__(cls, *args: object, **kwargs: object) -> OwnNew:
        return super().__new__(cls)


@dataclass
class OwnGetattribute:
    a: int

    def __getattribute__(self, name: str) -> object:
        return object.__getattribute__(self, name)


@dataclass
class OwnGetattr:
    a: int

    def __getattr__(self, name: str) -> object:
        raise AttributeError(name)


class Meta(type):
    pass


@dataclass
class WithMeta(metaclass=Meta):
    a: int


class Doubling:
    def __set_name__(self, owner: type, name: str) -> None:
        self.name = "_" + name

    def __get__(self, instance: object, owner: type | None = None) -> int:
        return 0 if instance is None else int(getattr(instance, self.name))

    def __set__(self, instance: object, value: int) -> None:
        setattr(instance, self.name, value * 2)


@dataclass
class DescriptorField:
    a: int = Doubling()  # type: ignore[assignment]


@dataclass
class Base:
    a: int


@dataclass(init=False)
class InheritsGeneratedInit(Base):
    b: int = 0


# Shapes that do rebuild exactly: accepted.
@dataclass
class WithClassVar:
    limit: ClassVar[int] = 5
    a: int


@dataclass
class Derived(Base):
    b: str


@dataclass(kw_only=True)
class KeywordOnly:
    a: int
    b: str = "x"


class Flag(IntFlag):
    A = 1


def tree(depth: int) -> Tree:
    node = Tree("leaf", [])
    for _ in range(depth):
        node = Tree("node", [node])
    return node


# Every kind of field a result type may have, with values at the edges of each kind.
ROUND_TRIPS: list[tuple[object, object]] = [
    (None, None),
    (None, type(None)),
    (True, bool),
    (False, bool),
    (0, int),
    (2**63 - 1, int),
    (-(2**63), int),
    (0.1, float),
    (-0.0, float),
    (1e300, float),
    (5e-324, float),
    ("", str),
    ('text, with [brackets] {braces}: "quotes" \\ and\nlines\r\x00\x1f\N{LINE SEPARATOR}', str),
    ("\N{GRINNING FACE} caf\N{LATIN SMALL LETTER E WITH ACUTE} \ud800", str),
    (b"", bytes),
    (bytes(range(256)) * 3, bytes),
    ([], list[int]),
    ([1, 2, 3], list[int]),
    ([[], [b"x"]], list[list[bytes]]),
    ((), tuple[int, ...]),
    ((1, 2), tuple[int, ...]),
    (("a", 1, None), tuple[str, int, None]),
    ((), tuple[()]),
    (None, int | None),
    (3, int | None),
    (None, Optional[str]),  # noqa: UP045 - the other spelling of the same type
    ("x", Optional[str]),  # noqa: UP045
    ([None, "a"], list[str | None]),
    (Empty(), Empty),
    (One(a=1), One),
    (Slotted(name="n", pages=(1, 2)), Slotted),
    (tree(10), Tree),
    (ProbeResult(size=3, head=b"abc"), ProbeResult),
    ([ProbePage(1, "a"), ProbePage(2, "b")], list[ProbePage]),
    (ProbePage(1, "a"), ProbePage | None),
    (WithClassVar(a=1), WithClassVar),
    (Derived(a=1, b="b"), Derived),
    (KeywordOnly(a=1), KeywordOnly),
    (sandbox_probes.every_kind(b"one\ntwo, three"), ProbeKinds),
    (sandbox_probes.every_kind(b""), ProbeKinds),
]


@pytest.mark.parametrize(("value", "result_type"), ROUND_TRIPS)
def test_every_declared_field_kind_round_trips(value: object, result_type: object) -> None:
    built = round_trip(value, result_type)
    assert built == value
    assert type(built) is type(value)


def test_bytes_and_tuples_come_back_as_declared() -> None:
    """A bytearray comes back as bytes, and an array as a tuple where a tuple is declared."""
    assert round_trip(bytearray(b"ab"), bytes) == b"ab"
    assert type(round_trip(bytearray(b"ab"), bytes)) is bytes
    assert round_trip([1, 2], tuple[int, ...]) == (1, 2)
    assert round_trip((1, 2), list[int]) == [1, 2]


def test_every_kind_comes_back_from_a_real_child() -> None:
    data = "first line, caf\N{LATIN SMALL LETTER E WITH ACUTE}\nsecond\r\nthird".encode()
    assert run("every_kind", data) == sandbox_probes.every_kind(data)


def test_the_answer_is_ascii_json() -> None:
    payload = sandbox_child.encode(sandbox_probes.every_kind("caf\N{GRINNING FACE}".encode()))
    assert payload.isascii()
    assert isinstance(json.loads(payload), dict)


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1},  # dict
        {1, 2},  # set
        frozenset(),
        object(),
        os.system,  # a function
        ProbeResult,  # a class
        float("nan"),
        float("inf"),
        Flag.A,  # an int subclass
        type("Text", (str,), {})("x"),  # a str subclass
        complex(1, 2),
        tree(sandbox_child.MAX_DEPTH),  # too deep
    ],
    ids=lambda value: type(value).__name__,
)
def test_the_child_refuses_to_encode_anything_else(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        sandbox_child.encode(value)


def test_a_recursive_value_is_refused_by_the_child() -> None:
    looped: list[object] = []
    looped.append(looped)
    with pytest.raises(ValueError, match="deep"):
        sandbox_child.encode(looped)


@pytest.mark.parametrize(
    "result_type",
    [
        sandbox_probes.echo,  # a vault function: what the finding named
        os.system,
        print,
        "vault.jobs.sandbox_probes:ProbeResult",  # a name, not a class
        ProbeResult(size=1, head=b""),  # an instance, not a class
        Plain,  # a class that is not a dataclass
        IntFlag,
        object,
        type,
        Any,
        dict,
        dict[str, int],
        set[int],
        frozenset[int],
        list,
        tuple,
        list[int, str],  # type: ignore[misc]
        int | str,
        int | str | None,
        Union[int, bytes],  # noqa: UP007 - the other spelling
        Literal[1],
        Annotated[int, "x"],
        complex,
        bytearray,
        WithDict,
        NotInitialised,
        Unresolvable,
        list[WithDict],
        Callable[[], int],
        WithInitVar,
        WithDefaultInitVar,
        OwnInit,
        InheritedInit,
        Checked,  # a __post_init__ that only checks is refused too: it cannot be told apart
        PostInitChanges,
        OwnSetattr,
        OwnNew,
        OwnGetattribute,
        OwnGetattr,
        WithMeta,
        DescriptorField,
        InheritsGeneratedInit,
        list[WithInitVar],
        ProbePage | PostInitChanges,
    ],
    ids=repr,
)
def test_a_result_type_that_is_not_allowed_is_refused_at_definition(result_type: object) -> None:
    with pytest.raises(ValueError, match="result type"):
        EntryPoint(f"{PROBES}:echo", result_type)


def test_an_entry_point_declares_its_result_type() -> None:
    with pytest.raises(TypeError):
        EntryPoint(f"{PROBES}:echo")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("payload", "result_type"),
    [
        # Something that names a type or a function: never resolved, refused by its shape.
        (b'{"__class__":"os:system","args":["calc"]}', ProbeResult),
        (b'{"py/object":"os.system"}', None),
        (b'{"size":1,"head":{"$bytes":""},"__reduce__":"os:system"}', ProbeResult),
        (b'{"$bytes":"","type":"vault.jobs.sandbox_probes:ProbeResult"}', bytes),
        (pickle.dumps(os.system, protocol=5), None),  # the old format
        (pickle.dumps(ProbeResult(size=1, head=b""), protocol=5), ProbeResult),
        # Another dataclass's shape, or a field missing, extra, repeated or of the wrong kind.
        (b'{"number":1,"text":"a"}', ProbeResult),
        (b'{"size":1}', ProbeResult),
        (b'{"size":1,"head":{"$bytes":""},"pages":2}', ProbeResult),
        (b'{"size":1,"size":2,"head":{"$bytes":""}}', ProbeResult),
        (b'{"size":"1","head":{"$bytes":""}}', ProbeResult),
        (b'[1,{"$bytes":""}]', ProbeResult),
        # Each kind against the others.
        (b"true", int),
        (b"1.0", int),
        (b'"1"', int),
        (b"1", float),
        (b"0", bool),
        (b"null", str),
        (b'{"$bytes":""}', str),
        (b'"YWJj"', bytes),
        (b'{"$bytes":"!!!!"}', bytes),
        (b'{"$bytes":"YQ"}', bytes),  # padding missing
        (b'{"$bytes":"YWJj","x":1}', bytes),
        (b'{"$bytes":1}', bytes),
        (b"[1]", tuple[int, int]),
        (b"[1,2,3]", tuple[int, int]),
        (b"[1,null]", list[int]),
        (b'"x"', int | None),
        (b"{}", list[int]),
        (b"[]", Empty),
        # Not JSON the child writes.
        (b"NaN", float),
        (b"Infinity", float),
        (b"-Infinity", float),
        (b"1" * 21, int),
        (b"-" + b"9" * 20, int),
        # Numbers whose literal is not the text the child writes for what they decode to.
        (b"1e999", float),  # overflows to inf
        (b"-1e999", float),  # to -inf
        (b"[1e999]", list[float]),
        (b'{"ratio":1e999}', None),
        (b"1e-400", float),  # underflows to 0.0
        (b"-1e-400", float),  # to -0.0
        (b"0.10000000000000001", float),  # rounds to 0.1: precision lost
        (b"9007199254740993.0", float),  # rounds to 9007199254740992.0
        (b"1E5", float),
        (b"1e5", float),  # the child writes 100000.0
        (b"1.50", float),
        (b"1.0e0", float),
        (b"1e+20", int),  # a float literal where an int is declared
        (b"-0", int),  # the child writes 0
        (b"1" * 25 + b".0", float),  # longer than any float the child writes
        (b"0." + b"0" * 400 + b"1", float),
        ('"caf\N{LATIN SMALL LETTER E WITH ACUTE}"'.encode(), str),  # not ASCII
        (b'"\xff"', str),
        (b"1 2", int),
        (b"", None),
        (b"not json", None),
        (b"\xef\xbb\xbfnull", None),  # a byte order mark
        ('"a"'.encode("utf-16"), str),
    ],
)
def test_an_answer_that_does_not_fit_the_declared_type_is_refused(
    payload: bytes, result_type: object
) -> None:
    with pytest.raises(SandboxResultError):
        decode(payload, result_type)


def test_nothing_in_the_answer_is_called_but_the_declared_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer shaped like another dataclass's fields builds the declared class or nothing."""
    called: list[str] = []
    real_init = ProbePage.__init__
    # Compiled first: a class whose __init__ is not the generated one is refused at definition.
    as_result, as_page = decoder(ProbeResult), decoder(ProbePage)
    answer = sandbox_child.OK + b'{"number":1,"text":"a"}'

    def recording_init(self: ProbePage, *args: object, **kwargs: object) -> None:
        called.append("ProbePage")
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ProbePage, "__init__", recording_init)
    with pytest.raises(SandboxResultError):
        sandbox._interpret(answer, as_result)
    assert called == []
    built = sandbox._interpret(answer, as_page)
    assert called == ["ProbePage"]
    assert built == ProbePage(1, "a")


def test_nesting_is_bounded_on_both_sides() -> None:
    deepest = tree(sandbox_child.MAX_DEPTH // 2 - 1)  # a Tree and its list: two levels each
    assert round_trip(deepest, Tree) == deepest
    with pytest.raises(ValueError, match="deep"):
        sandbox_child.encode(tree(sandbox_child.MAX_DEPTH // 2))
    # The same too-deep value written by hand, as a hijacked child would: refused by the server.
    too_deep = b'{"label":"leaf","children":[]}'
    for _ in range(sandbox_child.MAX_DEPTH // 2):
        too_deep = b'{"label":"node","children":[' + too_deep + b"]}"
    with pytest.raises(SandboxResultError):
        decode(too_deep, Tree)


@pytest.mark.parametrize("size", [100_000, 8 * MIB])
def test_deep_nesting_beyond_what_json_parses_is_refused_quickly(size: int) -> None:
    """json's scanner stops at CPython's recursion limit (``RecursionError``), whatever the
    length: refused, and the server neither crashes nor builds the rest."""
    started = time.monotonic()
    for payload in (b"[" * size + b"]" * size, b'{"a":' * size + b"1" + b"}" * size):
        with pytest.raises(SandboxResultError):
            decode(payload, Tree, max_memory=1 << 40, max_values=1 << 40)
    assert time.monotonic() - started < 30


NUMBERS: list[int | float] = [
    0, -1, 2**63 - 1, -(2**63), 10**19, -(10**18),
    0.0, -0.0, 0.1, -0.1, 1.5, 1e16, 1e20, 1e-7, 5e-324, -5e-324, 2.2250738585072014e-308,
    -2.2250738585072014e-308, 1.7976931348623157e308, -1.7976931348623157e308, 9007199254740993.0,
]  # fmt: skip


def random_floats(count: int) -> list[float]:
    """Finite floats from random bit patterns: every exponent and every mantissa is as likely."""
    import random
    import struct

    rng = random.Random(0)  # noqa: S311 - reproducible test data, not a secret
    values = (
        struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0] for _ in range(count * 2)
    )
    return [value for value in values if math.isfinite(value)][:count]


@pytest.mark.parametrize("value", NUMBERS + random_floats(2_000))
def test_every_number_the_child_writes_comes_back_exactly(value: int | float) -> None:
    payload = sandbox_child.encode(value)
    assert payload.decode("ascii") == sandbox_child.number_text(value)  # one rule, both sides
    built = decode(payload, type(value))
    assert type(built) is type(value)
    assert repr(built) == repr(value)  # bit for bit: -0.0 stays -0.0


@pytest.mark.parametrize(
    "value", [2**70, -(2**70), 10**20, float("inf"), float("-inf"), float("nan")], ids=repr
)
def test_the_child_refuses_a_number_the_server_would_not_read(value: int | float) -> None:
    with pytest.raises(ValueError, match=r"number|finite"):
        sandbox_child.encode(value)
    with pytest.raises(SandboxResultError):  # and the same literal written by hand is refused
        decode(repr(value).encode(), type(value))


@pytest.mark.parametrize(
    ("text", "kind"), [("0" * 21, int), ("1" * 25 + ".0", float), ("1" * 5_000, int)]
)
def test_a_long_literal_is_refused_by_its_length_before_it_is_read(
    text: str, kind: type[int] | type[float]
) -> None:
    """Refused for its length, not for the value it would read as: neither int() nor float()
    runs on more characters than the child ever writes."""
    with pytest.raises(ValueError, match="too long"):
        sandbox_child.read_number(text, kind)


def test_large_integers_are_refused_before_int_reads_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = sandbox._parse_int

    def recording(text: str) -> object:
        calls.append(text)
        return real(text)

    monkeypatch.setattr(sandbox, "_parse_int", recording)
    with pytest.raises(SandboxResultError):
        decode(b"1" * 4_000, int)
    assert calls == ["1" * 4_000]  # int() would read it (under CPython's 4300 digits): refused


# Answers that each build many objects for few bytes, with a type that accepts them.
HOSTILE: dict[str, tuple[bytes, object]] = {
    "empty lists": (b"[" + b"[]," * 50_000 + b"[]]", list[list[int]]),
    "empty objects": (b"[" + b"{}," * 50_000 + b"{}]", list[Empty]),
    "one-key objects": (b"[" + b'{"a":0},' * 50_000 + b'{"a":0}]', list[One]),
    "distinct keys": (b"[" + b",".join(b'{"k%d":0}' % n for n in range(50_000)) + b"]", None),
    "tuples": (b"[" + b"[0]," * 50_000 + b"[0]]", list[tuple[int, ...]]),
    "strings": (b"[" + b",".join(b'"%d"' % n for n in range(50_000)) + b"]", list[str]),
    "empty strings": (b"[" + b'"",' * 50_000 + b'""]', list[str]),
    "ints": (b"[" + b",".join(b"%d" % (n + 1000) for n in range(50_000)) + b"]", list[int]),
    "floats": (b"[" + b",".join(b"%d.5" % n for n in range(50_000)) + b"]", list[float]),
    "wide string": (b'"\\ud83d\\ude00' + b"a" * 1_000_000 + b'"', str),
    "one large bytes": (sandbox_child.encode(b"y" * 3_000_000), bytes),
    "many bytes": (sandbox_child.encode([b"%d" % n for n in range(30_000)]), list[bytes]),
    "nested": (b"[" + b"[[[0]]]," * 20_000 + b"[[[0]]]]", list[list[list[list[int]]]]),
    "dataclasses": (
        sandbox_child.encode([ProbeResult(size=n, head=b"") for n in range(20_000)]),
        list[ProbeResult],
    ),
    "every kind": (
        sandbox_child.encode([sandbox_probes.every_kind(b"a\nb") for _ in range(2_000)]),
        list[ProbeKinds],
    ),
}


def count_values(value: object) -> int:
    """The JSON values (keys included) a parsed answer holds: an independent count."""
    if isinstance(value, list):
        return 1 + sum(count_values(item) for item in value)
    if isinstance(value, dict):
        return 1 + sum(1 + count_values(item) for item in value.values())
    return 1


@pytest.mark.parametrize(("payload", "result_type"), HOSTILE.values(), ids=HOSTILE.keys())
def test_the_value_count_bounds_what_json_builds(payload: bytes, result_type: object) -> None:
    built = json.loads(payload)
    assert sandbox._answer_cost(sandbox_child.OK + payload)[1] >= count_values(built)


@pytest.mark.parametrize(("payload", "result_type"), HOSTILE.values(), ids=HOSTILE.keys())
def test_the_cost_covers_what_decoding_allocates(payload: bytes, result_type: object) -> None:
    """Compared with an independent measure: tracemalloc's peak while the server decodes the
    answer and builds the declared type, so the constants cannot drift below what CPython
    allocates."""
    answer = sandbox_child.OK + payload
    reader = decoder(result_type)
    cost, _values = sandbox._answer_cost(answer)
    tracemalloc.start()
    try:
        with contextlib.suppress(SandboxResultError):
            sandbox._load_result(answer, reader, 1 << 40, 1 << 40)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert cost >= peak


def test_an_answer_that_could_build_too_much_is_refused_before_it_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Small on the wire, large in the server: refused by the value count and by the memory
    budget, without json ever running."""
    payload = b"[" + b"[]," * 2_000 + b"[]]"
    monkeypatch.setattr(json, "loads", never)
    with pytest.raises(SandboxResultError):
        decode(payload, list[list[int]], max_values=1_000)
    with pytest.raises(SandboxResultError):
        decode(payload, list[list[int]], max_memory=100_000)
    with pytest.raises(SandboxResultError):
        decode(b'"' + b"x" * 200_000 + b'"', str, max_memory=100_000)
    monkeypatch.undo()
    monkeypatch.setattr(json, "loads", never)
    with pytest.raises(ParserRanError):  # the control: within the limits, json does run
        decode(payload, list[list[int]])


def test_an_answer_within_the_budget_is_built() -> None:
    payload = b"[" + b"[]," * 1_999 + b"[]]"
    assert decode(payload, list[list[int]], max_values=4_001) == [[]] * 2_000
    assert decode(payload, list[list[int]], max_memory=1_100_000) == [[]] * 2_000


def test_the_answer_and_the_load_are_charged_before_any_value() -> None:
    cost, values = sandbox._answer_cost(sandbox_child.OK + b"null")
    assert values == 1
    assert cost == sandbox._LOAD_BYTES + 4 * sandbox._ANSWER_BYTE_COST + sandbox._VALUE_COST
    with pytest.raises(SandboxResultError):
        decode(b"null", None, max_memory=cost - 1)
    assert decode(b"null", None, max_memory=cost) is None


def test_the_budget_applies_to_a_real_child() -> None:
    limits = SandboxLimits(timeout_seconds=30, memory_bytes=256 * MIB, max_result_values=1_000)
    with pytest.raises(SandboxResultError):
        run("many_lists", b"1000", limits)
    assert run("many_lists", b"100", limits) == [[]] * 100


def test_the_default_budget_refuses_a_hostile_answer_of_the_largest_size_quickly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """256 MiB of ``[],`` would build about 6 GiB; the default limits refuse it from its bytes,
    without parsing, in a bounded time."""
    count = (sandbox.DEFAULT_MAX_RESULT_BYTES - 2) // 3
    answer = sandbox_child.OK + b"[" + b"[]," * count + b"0]"
    monkeypatch.setattr(json, "loads", never)
    started = time.monotonic()
    with pytest.raises(SandboxResultError):
        sandbox._interpret(answer, decoder(list[list[int]]))
    assert time.monotonic() - started < 30


def test_a_large_answer_is_read_without_copying_the_buffer() -> None:
    """The text is decoded from a view of the answer buffer, which a bytearray also gives."""
    payload = sandbox_child.encode("x" * 100_000)
    assert sandbox._interpret(bytearray(sandbox_child.OK + payload), decoder(str)) == "x" * 100_000


@pytest.mark.parametrize(
    "answer",
    [
        sandbox_child.OK + b"not json",
        sandbox_child.OK,
        sandbox_child.MEMORY + b"extra",
        b"Z",
        sandbox_child.UNENCODABLE,
    ],
)
def test_a_malformed_answer_is_a_result_error(answer: bytes) -> None:
    with pytest.raises(SandboxResultError):
        sandbox._interpret(answer, decoder(None))


def test_an_error_name_that_is_not_a_class_name_is_replaced() -> None:
    with pytest.raises(SandboxChildError) as caught:
        sandbox._interpret(sandbox_child.ERROR + b"Jane Roe\n", decoder(None))
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
        sandbox._interpret(answer, decoder(None))


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
        EntryPoint(target, None)
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
    entries = {"x": EntryPoint(f"{PROBES}:imported_function", None)}
    with pytest.raises(SandboxRefusedError):
        sandbox.run("x", b"", LIMITS, entry_points=entries)


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
        b'{"$bytes":"YWI="}',
    )
    assert answer(f"{PROBES}:fail", SECRET.encode()) == b"EValueError"
    assert answer(f"{PROBES}:fail_with_memory", b"x") == sandbox_child.MEMORY
    assert answer(f"{PROBES}:unencodable", b"") == sandbox_child.UNENCODABLE
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
        if callable(value)
        and getattr(value, "__module__", None) == PROBES
        and name.islower()
        and not name.startswith("_")
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
        (
            {"timeout_seconds": 1, "memory_bytes": 256 * MIB, "max_result_memory_bytes": 0},
            ValueError,
        ),
        ({"timeout_seconds": 1, "memory_bytes": 256 * MIB, "max_result_values": 0}, ValueError),
        ({"timeout_seconds": 1, "memory_bytes": 256 * MIB, "max_result_values": 1.5}, TypeError),
        ({"timeout_seconds": "1", "memory_bytes": 256 * MIB}, TypeError),
    ],
)
def test_limits_are_checked(kwargs: dict[str, object], error: type[Exception]) -> None:
    with pytest.raises(error):
        SandboxLimits(**kwargs)  # type: ignore[arg-type]
