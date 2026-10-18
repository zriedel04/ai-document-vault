"""Functions that exercise the sandbox runner (:mod:`vault.jobs.sandbox`) in its tests.

They live in the package because the child starts in isolated mode (``python -I``) and imports
only installed modules; a function in ``tests/`` would not be found there. None of them is in
``sandbox.ENTRY_POINTS``, so the server never runs them: a test passes its own allowlist.
Each takes the input bytes, as every entry point does.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict as imported_function  # noqa: F401 - a test needs a re-export
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProbeResult:
    size: int
    head: bytes


def echo(data: bytes) -> bytes:
    return data


def describe(data: bytes) -> ProbeResult:
    return ProbeResult(size=len(data), head=data[:4])


def fail(data: bytes) -> None:
    """Raise with the input in the message, as a parser quoting a document would."""
    raise ValueError(data.decode("utf-8"))


def fail_with_memory(data: bytes) -> None:
    raise MemoryError(data.decode("utf-8"))


def noisy(data: bytes) -> int:
    """Print, write and log the input, which must never reach the server's console."""
    text = data.decode("utf-8")
    print(text)
    print(text, file=sys.stderr)
    os.write(1, data)
    os.write(2, data)
    logging.getLogger("some.library").error("%s", text)
    return len(data)


def allocate(data: bytes) -> int:  # pragma: no cover - runs in the child
    """Allocate as many bytes as the input says, and touch them."""
    block = bytearray(int(data))
    block[::4096] = b"\x01" * len(block[::4096])
    return len(block)


def sleep(data: bytes) -> None:  # pragma: no cover - runs in the child
    time.sleep(float(data))


def sleep_with_pid_file(data: bytes) -> None:  # pragma: no cover - runs in the child
    """Write this process's id to the file the input names, then sleep."""
    Path(data.decode("utf-8")).write_text(str(os.getpid()), encoding="ascii")
    time.sleep(60)


def spawn_sleeper(data: bytes) -> None:  # pragma: no cover - runs in the child
    """Start a grandchild that sleeps, write its id to the file the input names, then sleep."""
    sleeper = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(data.decode("utf-8")).write_text(str(sleeper.pid), encoding="ascii")
    time.sleep(60)


def exit_now(data: bytes) -> None:  # pragma: no cover - runs in the child
    os._exit(int(data))


def abort(data: bytes) -> None:  # pragma: no cover - runs in the child
    os.abort()


def unpicklable(data: bytes) -> object:
    return lambda: data


def large(data: bytes) -> bytes:
    return b"x" * int(data)


def environment_names(data: bytes) -> list[str]:
    return sorted(os.environ)


def exit_system(data: bytes) -> None:
    sys.exit(data.decode("utf-8"))
