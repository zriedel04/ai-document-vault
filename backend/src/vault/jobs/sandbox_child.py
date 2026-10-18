"""The child side of the sandbox runner (:mod:`vault.jobs.sandbox`).

The parent starts ``python -I -m vault.jobs.sandbox_child TARGET MEMORY_BYTES PARENT_PID``: the
command line holds only the target's name (fixed in code), the memory cap and the parent's process
id, never document content. The child then

1. keeps its standard output for the answer and points file descriptors 1 and 2 at the null
   device, so nothing a library prints or logs reaches the server's console (the parent starts it
   with standard error on the null device too);
2. on Linux, asks to be killed when the parent dies and caps its address space with
   ``RLIMIT_AS`` (on Windows the parent puts it in a Job Object instead, before sending input);
3. reads the input bytes from standard input until the end;
4. calls the target and writes one answer: a status byte followed by the pickled result, or by
   the exception's class name, and nothing else.

Only the class name of an exception leaves the child, never its message or traceback, which can
quote the document. Standard library only, and nothing else of the package is imported, so a
child starts fast and holds little before its target runs.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pickle
import re
import sys
from typing import Final

# The answer's first byte.
OK: Final = b"R"  # the pickled result follows
ERROR: Final = b"E"  # the target raised; the exception's class name follows (ASCII)
MEMORY: Final = b"M"  # the target or the pickling ran out of memory
UNPICKLABLE: Final = b"U"  # the result could not be pickled
REFUSED: Final = b"X"  # the target is not a top-level function of a ``vault.`` module
SETUP: Final = b"S"  # the child could not limit itself

# Always ``fullmatch``: ``$`` would also match before a trailing newline.
TARGET: Final = re.compile(r"vault(?:\.[a-z_][a-z0-9_]*)+:[a-z_][a-z0-9_]*")
ERROR_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
UNNAMED_ERROR: Final = "Error"
PICKLE_PROTOCOL: Final = 5
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
    except Exception:
        return None
    if (
        not inspect.isfunction(function)
        or function.__module__ != module_name
        or function.__qualname__ != name
    ):
        return None
    return function


def respond(target: str, data: bytes) -> bytes:
    """Run ``target`` on ``data`` and return the answer: a status byte and its payload."""
    function = resolve(target)
    if function is None:
        return REFUSED
    try:
        result = function(data)  # type: ignore[operator]
    except MemoryError:
        return MEMORY
    except BaseException as error:  # everything, SystemExit too: report it, never its message
        return ERROR + error_name(error).encode("ascii")
    try:
        payload = pickle.dumps(result, protocol=PICKLE_PROTOCOL)
    except MemoryError:
        return MEMORY
    except BaseException:
        return UNPICKLABLE
    return OK + payload


def _write_all(fd: int, data: bytes) -> None:  # pragma: no cover - runs in the child process
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _read_all(fd: int) -> bytes:  # pragma: no cover - runs in the child process
    parts = []
    while chunk := os.read(fd, _READ_CHUNK):
        parts.append(chunk)
    return b"".join(parts)


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
    """Die with the parent (``PR_SET_PDEATHSIG``) and cap the address space. False on failure.

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
    except (OSError, ValueError, AttributeError):
        return False
    return True


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
        target, memory, parent = argv
        memory_bytes, parent_pid = int(memory), int(parent)
    except ValueError:
        _write_all(answer_fd, SETUP)
        os._exit(2)
    if sys.platform == "linux" and not _limit_linux(memory_bytes, parent_pid):
        _write_all(answer_fd, SETUP)
        os._exit(0)
    data = _read_all(0)
    _write_all(answer_fd, respond(target, data))
    os.close(answer_fd)
    # Skip interpreter shutdown: a library's exit hook could print or wait on its own threads.
    os._exit(0)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
