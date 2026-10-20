"""The child side of the sandbox runner (:mod:`vault.jobs.sandbox`).

The parent starts ``python -I -m vault.jobs.sandbox_child TARGET MEMORY_BYTES PARENT_PID
INPUT_BYTES``: the command line holds only the target's name (fixed in code), the memory cap, the
parent's process id and the input's length, never document content. The child then

1. keeps its standard output for the answer and points file descriptors 1 and 2 at the null
   device, so nothing a library prints or logs reaches the server's console (the parent starts it
   with standard error on the null device too);
2. on Linux, asks to be killed when the parent dies, caps its address space with ``RLIMIT_AS``
   and installs a seccomp filter that refuses to start another process, since each process
   would get a cap of its own (on Windows the parent puts it in a Job Object instead, whose cap
   is the total of every process in it, before sending input);
3. reads the input bytes from standard input into one ``bytes`` object of the announced length
   (one allocation, no second copy: the input may be close to the memory cap);
4. calls the target and writes one answer: a status byte followed by the result as ASCII JSON
   (:func:`encode`), or by the exception's class name, and nothing else.

Running out of memory at any step that grows with the input or the result (reading the input,
importing and running the target, encoding the result, writing the answer) is reported as
``MEMORY``, never as a crash or a refusal. Only if even that byte cannot be written does the child
end without an answer (:class:`~vault.jobs.sandbox.SandboxCrashError`).

Only the class name of an exception leaves the child, never its message or traceback, which can
quote the document. Standard library only, and nothing else of the package is imported, so a
child starts fast and holds little before its target runs.
"""

from __future__ import annotations

import base64
import dataclasses
import importlib
import inspect
import io
import json
import math
import os
import re
import sys
from typing import Final

# The answer's first byte.
OK: Final = b"R"  # the result follows, as ASCII JSON
ERROR: Final = b"E"  # the target raised; the exception's class name follows (ASCII)
MEMORY: Final = b"M"  # the target or the encoding ran out of memory
UNENCODABLE: Final = b"U"  # the result holds a value encode() does not write
REFUSED: Final = b"X"  # the target is not a top-level function of a ``vault.`` module
SETUP: Final = b"S"  # the child could not limit itself

# Always ``fullmatch``: ``$`` would also match before a trailing newline.
TARGET: Final = re.compile(r"vault(?:\.[a-z_][a-z0-9_]*)+:[a-z_][a-z0-9_]*")
ERROR_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
UNNAMED_ERROR: Final = "Error"
# How bytes are written: an object whose only key is this (not a field name: it is not an
# identifier).
BYTES_KEY: Final = "$bytes"
# How deep containers (lists, tuples, dataclasses) may nest in a result; the parent refuses deeper.
MAX_DEPTH: Final = 64
# The longest number literals written or read, sign included: every 64-bit integer fits in 20,
# and the longest ``repr`` of a finite float is 24 (``-2.2250738585072014e-308``). A longer
# literal is refused before ``int()`` or ``float()`` reads it.
MAX_INT_CHARACTERS: Final = 20
MAX_FLOAT_CHARACTERS: Final = 24


def number_text(value: int | float) -> str:
    """The text ``json.dumps`` writes for an ``int`` or ``float`` (its ``repr``), or
    ``ValueError`` for a number the sandbox does not bring back: a float that is not finite, or
    a literal longer than ``MAX_INT_CHARACTERS`` or ``MAX_FLOAT_CHARACTERS``.

    The one rule for numbers: :func:`encode` writes only what this accepts, and the parent reads
    a literal only if it is this function's text for the value it reads back
    (:func:`read_number`), so every number that crosses round-trips exactly.
    """
    if type(value) is int:
        text, limit = int.__repr__(value), MAX_INT_CHARACTERS
    elif type(value) is float and math.isfinite(value):
        text, limit = float.__repr__(value), MAX_FLOAT_CHARACTERS
    else:
        msg = "not a finite int or float"
        raise ValueError(msg)
    if len(text) > limit:
        msg = "the number is too long"
        raise ValueError(msg)
    return text


def read_number(text: str, kind: type[int] | type[float]) -> int | float:
    """The number a JSON literal stands for, or ``ValueError`` unless ``text`` is exactly what
    :func:`number_text` writes for it. This refuses a literal that overflows (``1e999`` is
    ``inf``) or underflows (``1e-400`` is ``0.0``), loses precision (``0.10000000000000001``),
    or is spelled another way (``1E5``, ``1.50``, ``-0``) than the child writes it."""
    limit = MAX_INT_CHARACTERS if kind is int else MAX_FLOAT_CHARACTERS
    if len(text) > limit:
        msg = "the number is too long"
        raise ValueError(msg)
    value = int(text) if kind is int else float(text)
    if number_text(value) != text:
        msg = "not the text the child writes for this number"
        raise ValueError(msg)
    return value


_READ_CHUNK: Final = 1 << 16


def error_name(error: BaseException) -> str:
    """The exception's class name, or ``Error`` if it is not a plain ASCII name."""
    name = type(error).__name__
    return name if ERROR_NAME.fullmatch(name) else UNNAMED_ERROR


def resolve(target: str) -> object | None:
    """The function ``target`` names, or None when it is not one the sandbox may call.

    The parent checks its allowlist first; this checks again on this side: the name has the
    ``vault.module:function`` form, and the object is a plain function defined at the top level
    of that module, not a class, a builtin or a function another module's name was imported as.
    """
    if not isinstance(target, str) or not TARGET.fullmatch(target):
        return None
    module_name, name = target.split(":")
    try:
        module = importlib.import_module(module_name)
        function = getattr(module, name)
    except MemoryError:
        raise  # out of memory is not a refusal: respond() reports it
    except Exception:
        return None
    if (
        not inspect.isfunction(function)
        or function.__module__ != module_name
        or function.__qualname__ != name
    ):
        return None
    return function


def _plain(value: object, depth: int) -> object:
    """``value`` as what ``json.dumps`` writes, or ``TypeError``/``ValueError`` if it holds a value
    the sandbox does not bring back. Exact types only: a subclass (an ``IntEnum``, a ``str``
    subclass) or a dict is refused rather than guessed at."""
    kind = type(value)
    if value is None or kind is bool or kind is str:
        return value
    if kind is int or kind is float:
        number_text(value)  # type: ignore[arg-type]  # refuses what the parent would not read
        return value
    if kind is bytes or kind is bytearray:
        return {BYTES_KEY: base64.b64encode(value).decode("ascii")}  # type: ignore[arg-type]
    if depth >= MAX_DEPTH:
        msg = "the result is nested too deep"
        raise ValueError(msg)
    if kind is list or kind is tuple:
        return [_plain(item, depth + 1) for item in value]  # type: ignore[attr-defined]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _plain(getattr(value, item.name), depth + 1)
            for item in dataclasses.fields(value)
        }
    raise TypeError


def encode(result: object) -> bytes:
    """The result as JSON in ASCII: ``None``, booleans, integers, finite floats and strings as
    themselves, ``bytes`` and ``bytearray`` as ``{"$bytes": "<base64>"}``, lists and tuples as
    arrays, and dataclass instances as objects of their fields, nested up to ``MAX_DEPTH``. The
    parent builds the entry point's declared type from it; nothing in it names a type. Raises
    ``TypeError`` or ``ValueError`` (a ``NaN``, too deep, any other value) when it cannot."""
    text = json.dumps(
        _plain(result, 0),
        ensure_ascii=True,
        allow_nan=False,
        check_circular=False,  # _plain already bounded the depth, and built no shared objects
        separators=(",", ":"),
    )
    return text.encode("ascii")


def respond(target: str, data: bytes) -> tuple[bytes, bytes]:
    """Run ``target`` on ``data`` and return the answer: a status byte and its payload.

    They are kept apart so that the payload, as large as the result, is never copied again to put
    the status byte in front of it.
    """
    try:
        function = resolve(target)
    except MemoryError:
        return MEMORY, b""
    if function is None:
        return REFUSED, b""
    try:
        result = function(data)  # type: ignore[operator]
    except MemoryError:
        return MEMORY, b""
    except BaseException as error:  # everything, SystemExit too: report it, never its message
        return ERROR, error_name(error).encode("ascii")
    try:
        payload = encode(result)
    except MemoryError:
        return MEMORY, b""
    except BaseException:
        return UNENCODABLE, b""
    return OK, payload


def read_input(stream: io.RawIOBase, length: int) -> bytes | None:
    """Read exactly ``length`` bytes into one ``bytes`` object, or None if the input is shorter.

    ``BufferedReader.read(n)`` allocates its result once and reads into it, so the input is held
    once; joining chunks would hold it twice, and the input may be close to the memory cap.
    Raises ``MemoryError`` when even one copy does not fit.
    """
    if not length:
        return b""
    reader = io.BufferedReader(stream, buffer_size=_READ_CHUNK)
    try:
        data = reader.read(length)
    finally:
        reader.detach()  # the caller owns the stream; closing the reader would close it
    return data if len(data) == length else None


def write_answer(fd: int, status: bytes, payload: bytes) -> None:
    """Write the status byte, then the payload from a view of it (no copy)."""
    for part in (status, payload):
        view = memoryview(part)
        while view:
            written = os.write(fd, view)
            view = view[written:]


def _silence_output() -> int:  # pragma: no cover - runs in the child process
    """Keep standard output for the answer, as a new descriptor, and send 1 and 2 nowhere."""
    answer = os.dup(1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives as long as the child
    sys.stderr = sys.stdout
    return answer


def _limit_linux(memory_bytes: int, parent_pid: int) -> bool:  # pragma: no cover - child only
    """Die with the parent (``PR_SET_PDEATHSIG``), cap the address space and forbid starting
    another process (:func:`_forbid_new_processes`), so the cap is the total. False on failure.

    The death signal is sent when the parent thread that started the child ends; the runner's
    call is synchronous, so that thread outlives the child. A parent that died before the call
    is caught by comparing the parent id with the one on the command line.
    """
    if sys.platform != "linux":
        return False
    import ctypes
    import resource
    import signal

    pr_set_pdeathsig = 1
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(pr_set_pdeathsig, signal.SIGKILL, 0, 0, 0) != 0:
            return False
        if os.getppid() != parent_pid:
            os._exit(1)
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        cap = memory_bytes if hard == resource.RLIM_INFINITY else min(memory_bytes, hard)
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        return _forbid_new_processes()
    except (OSError, ValueError, AttributeError):
        return False


# Linux system call numbers and audit architectures for the no-new-process filter
# (``_forbid_new_processes``). ``None``: the call does not exist on that architecture.
_AUDIT_ARCH_X86_64: Final = 0xC000003E
_AUDIT_ARCH_AARCH64: Final = 0xC00000B7
_SYSCALLS: Final = {
    # machine: (audit arch, clone, clone3, fork, vfork)
    "x86_64": (_AUDIT_ARCH_X86_64, 56, 435, 57, 58),
    "aarch64": (_AUDIT_ARCH_AARCH64, 220, 435, None, None),
}
_X32_SYSCALL_BIT: Final = 0x40000000
_CLONE_THREAD: Final = 0x00010000
_EPERM: Final = 1
_ENOSYS: Final = 38


def no_new_process_filter(machine: str) -> list[tuple[int, int, int, int]] | None:
    """A classic BPF seccomp program that refuses to create a process, or None for an
    architecture it does not know.

    It allows every system call but these: ``fork`` and ``vfork`` fail with ``EPERM``, ``clone``
    fails with ``EPERM`` unless its flags hold ``CLONE_THREAD`` (a new thread shares the process's
    memory, so its address space limit too), and ``clone3``, whose flags sit in memory a filter
    cannot read, fails with ``ENOSYS``, on which glibc starts threads with ``clone`` instead. A
    call from another architecture's ABI (32-bit ``int 0x80``, x32) fails with ``EPERM``, so its
    own numbers for ``fork`` cannot get around the filter. Each instruction is
    ``(code, jump if true, jump if false, k)`` as ``struct sock_filter`` holds it.
    """
    numbers = _SYSCALLS.get(machine)
    if numbers is None:
        return None
    arch, clone, clone3, fork, vfork = numbers
    ld_w_abs, jeq_k, jge_k, jset_k, ret_k = 0x20, 0x15, 0x35, 0x45, 0x06
    allow, errno = 0x7FFF0000, 0x00050000  # SECCOMP_RET_ALLOW, SECCOMP_RET_ERRNO
    deny, unsupported = errno | _EPERM, errno | _ENOSYS
    refused = [number for number in (fork, vfork) if number is not None]
    program: list[tuple[int, int, int, int]] = [
        (ld_w_abs, 0, 0, 4),  # seccomp_data.arch
        (jeq_k, 1, 0, arch),
        (ret_k, 0, 0, deny),
        (ld_w_abs, 0, 0, 0),  # seccomp_data.nr
        (jge_k, 0, 1, _X32_SYSCALL_BIT),
        (ret_k, 0, 0, deny),
        (jeq_k, 0, 1, clone3),
        (ret_k, 0, 0, unsupported),
    ]
    for number in refused:
        program += [(jeq_k, 0, 1, number), (ret_k, 0, 0, deny)]
    program += [
        (jeq_k, 1, 0, clone),
        (ret_k, 0, 0, allow),
        (ld_w_abs, 0, 0, 16),  # the low 32 bits of seccomp_data.args[0], the flags
        (jset_k, 0, 1, _CLONE_THREAD),
        (ret_k, 0, 0, allow),
        (ret_k, 0, 0, deny),
    ]
    return program


def _forbid_new_processes() -> bool:  # pragma: no cover - runs in the child process
    """Install :func:`no_new_process_filter` on this process. False when it cannot be.

    ``RLIMIT_AS`` caps one process, and a process it starts gets a cap of its own, so the
    memory cap is a total only while the child is the only process: with this filter it stays
    so, as the Job Object keeps it a total on Windows. It also keeps every process the sandbox
    runs in its process group and under the parent-death signal. ``PR_SET_NO_NEW_PRIVS`` comes
    first, as the kernel requires of a process without ``CAP_SYS_ADMIN``; a filter cannot be
    removed once installed, and it is inherited by nothing, since nothing can be started.
    """
    import ctypes
    import platform

    program = no_new_process_filter(platform.machine())
    if program is None:
        return False

    class _Instruction(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_uint16),
            ("jt", ctypes.c_uint8),
            ("jf", ctypes.c_uint8),
            ("k", ctypes.c_uint32),
        ]

    class _Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_Instruction))]

    pr_set_no_new_privs, pr_set_seccomp, seccomp_mode_filter = 38, 22, 2
    instructions = (_Instruction * len(program))(*(_Instruction(*item) for item in program))
    fprog = _Program(len(program), instructions)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, *[ctypes.c_ulong] * 4]
    if libc.prctl(pr_set_no_new_privs, 1, 0, 0, 0) != 0:
        return False
    installed = libc.prctl(pr_set_seccomp, seccomp_mode_filter, ctypes.addressof(fprog), 0, 0)
    return bool(installed == 0)


def _prepare_windows(answer_fd: int) -> None:  # pragma: no cover - Windows only, child only
    """No error dialogs, and no newline translation on the input or the answer.

    A crash must end the child, not open a Windows error dialog that waits for a click. The C
    runtime may open a descriptor in text mode, which would turn ``\r\n`` into ``\n`` and stop
    at a ``0x1A`` byte, so both pipes are switched to binary mode.
    """
    if sys.platform == "win32":
        import ctypes
        import msvcrt

        sem_failcriticalerrors, sem_nogpfaulterrorbox, sem_noopenfileerrorbox = 0x1, 0x2, 0x8000
        ctypes.windll.kernel32.SetErrorMode(
            sem_failcriticalerrors | sem_nogpfaulterrorbox | sem_noopenfileerrorbox
        )
        msvcrt.setmode(0, os.O_BINARY)
        msvcrt.setmode(answer_fd, os.O_BINARY)


def main(argv: list[str]) -> None:  # pragma: no cover - runs in the child process
    answer_fd = _silence_output()
    _prepare_windows(answer_fd)
    try:
        target, memory, parent, size = argv
        memory_bytes, parent_pid, length = int(memory), int(parent), int(size)
    except ValueError:
        write_answer(answer_fd, SETUP, b"")
        os._exit(2)
    if sys.platform == "linux" and not _limit_linux(memory_bytes, parent_pid):
        write_answer(answer_fd, SETUP, b"")
        os._exit(0)
    try:
        data = read_input(io.FileIO(0, closefd=False), length)
        if data is None:  # the parent stopped sending: no answer, so the parent sees a crash
            os._exit(3)
        status, payload = respond(target, data)
        del data
        write_answer(answer_fd, status, payload)
    except MemoryError:
        # Nothing has been written yet when reading or respond() runs out; a failure part way
        # through writing the answer leaves bytes the parent refuses as malformed.
        write_answer(answer_fd, MEMORY, b"")
    os.close(answer_fd)
    # Skip interpreter shutdown: a library's exit hook could print or wait on its own threads.
    os._exit(0)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
