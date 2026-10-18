"""The child side of the sandbox runner (:mod:`vault.jobs.sandbox`).

The parent starts ``python -I -m vault.jobs.sandbox_child TARGET MEMORY_BYTES PARENT_PID
INPUT_BYTES``: the command line holds only the target's name (fixed in code), the memory cap, the
parent's process id and the input's length, never document content. The child then

1. keeps its standard output for the answer and points file descriptors 1 and 2 at the null
   device, so nothing a library prints or logs reaches the server's console (the parent starts it
   with standard error on the null device too);
2. on Linux, asks to be killed when the parent dies and caps its address space with
   ``RLIMIT_AS`` (on Windows the parent puts it in a Job Object instead, before sending input);
3. reads the input bytes from standard input into one ``bytes`` object of the announced length
   (one allocation, no second copy: the input may be close to the memory cap);
4. calls the target and writes one answer: a status byte followed by the pickled result, or by
   the exception's class name, and nothing else.

Running out of memory at any step that grows with the input or the result (reading the input,
importing and running the target, pickling the result, writing the answer) is reported as
``MEMORY``, never as a crash or a refusal. Only if even that byte cannot be written does the child
end without an answer (:class:`~vault.jobs.sandbox.SandboxCrashError`).

Only the class name of an exception leaves the child, never its message or traceback, which can
quote the document. Standard library only, and nothing else of the package is imported, so a
child starts fast and holds little before its target runs.
"""

from __future__ import annotations

import importlib
import inspect
import io
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
        payload = pickle.dumps(result, protocol=PICKLE_PROTOCOL)
    except MemoryError:
        return MEMORY, b""
    except BaseException:
        return UNPICKLABLE, b""
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
