"""The sandbox runner: heavy parsing in a child process with a timeout and a memory cap.

The plan (``docs/architecture.md`` sections 3, 7 and 10) and ADR 0005 run parsing in a child
process, so a malformed file cannot hang, exhaust or crash the server. :func:`run` calls one named
function on bytes in a fresh ``python -I -m vault.jobs.sandbox_child`` and returns its result, or
raises one :class:`SandboxError` subclass that says how it failed:

- :class:`SandboxTimeoutError`: it ran past the timeout; the child and everything it started are
  killed and reaped;
- :class:`SandboxMemoryLimitError`: an allocation failed at the memory cap;
- :class:`SandboxCrashError`: the child ended without an answer (a non-zero exit code, a signal, or
  ``os._exit(0)``);
- :class:`SandboxChildError`: the function raised; only the exception's class name comes back
  (``error_class``), never its message or traceback, which can quote the document;
- :class:`SandboxResultError`: the result could not be pickled, was larger than the cap, held a
  type that is not allowed for this entry point, or was not a well-formed answer;
- :class:`SandboxRefusedError`: the entry point is not on the allowlist (nothing is started);
- :class:`SandboxUnavailableError`: the child could not be started or limited on this system.

Every message is fixed text, so a job's error (the class name, ``JobQueue.fail``) and any log line
say what happened without quoting anything.

**Allowlist.** Only entry points in :data:`ENTRY_POINTS` (or a mapping the caller passes) run,
each a top-level function of a ``vault.`` module, ``vault.ingest.extractors.pdf:extract`` say. The
child checks again that the name is a plain function defined in that module.

**Input and answer.** The input goes to the child's standard input through a pipe and never
appears on its command line; no temporary file is written, so there is no file a link could
redirect. The child answers on its standard output, which the parent reads up to
``max_result_bytes``; its standard error goes to the null device, and the child points its own
descriptors 1 and 2 there too, so nothing a library prints reaches the server's console. The
result is unpickled by an unpickler that resolves only the classes listed for the entry point
(plain values such as ``str``, ``bytes``, numbers, lists, tuples, dicts and sets need none), so a
child that a hostile file took over cannot make the server run code through the pickle. Nor can it
make the server build more than it should: before unpickling, the parent reads the answer's
opcodes without building anything and refuses an answer whose objects would cost more than
``max_result_memory_bytes`` (1 GiB by default) or that has more than ``max_result_opcodes``
opcodes (4 million), because a pickle of 256 MiB made of one-byte "empty list" opcodes would
otherwise build about 20 GiB in the server. It accepts only the opcodes protocol 5 writes for a
result, so a memo index named in the stream (nine bytes of ``LONG_BINPUT`` would grow the memo
table to gigabytes) is refused rather than charged. The child
gets a reduced environment (:data:`CHILD_ENV`), without API keys or other secrets.

**Memory cap.** On Windows the child is put in a Job Object with a process and a job memory limit
(committed memory) and kill-on-close: when the server process ends for any reason, Windows closes
the job's handle and ends every process in it, so a crash of the server leaves no orphan. The
child starts suspended and runs only once it is in the job: in a virtual environment
``sys.executable`` is a launcher that starts the real interpreter as its own child, and a process
started before its parent joined the job would stay outside it. Processes started inside the job
are in it too, so the cap and the kill cover the real interpreter and anything it starts. Before
the first child, the server puts itself in a kill-on-close job of its own (:func:`server_job`), so
every child is in that job from the moment it is created: a server that dies while a child is
still suspended and outside its own job takes the child with it. ``subprocess`` cannot create a
process directly in a job (``PROC_THREAD_ATTRIBUTE_JOB_LIST``), which would be the other way to
close that window. On Linux the child caps its own address space with ``RLIMIT_AS`` before it
reads the input, and asks the kernel to kill it when the parent dies
(``PR_SET_PDEATHSIG``), then checks that its parent is still the server, so a server that died
before that call is caught too; until the child has its input it waits on a pipe that the
server's death closes. Address space is larger than committed memory (libraries reserve more
than they use), so the same number is stricter on Linux; the Linux path is for development and
CI, the product runs on Windows. An allocation that fails raises ``MemoryError`` in the child on
both systems, and the child reports it as :class:`SandboxMemoryLimitError` at every step that grows
with the input or the result: reading the input (into one ``bytes`` of the length given on its
command line, so it is held once), importing and running the target, pickling the result and
writing the answer. A library that aborts instead shows up as :class:`SandboxCrashError`. The
parent passes the input as a view and reads the answer into one buffer bounded by
``max_result_bytes``, unpickling from a view of it, so neither is copied again. Other systems are
not supported (:class:`SandboxUnavailableError`).

**Timeout.** The timeout covers the whole run, the child's start included. On expiry the parent
ends the child's whole process group (Linux, ``start_new_session``) or job (Windows), then reaps
the child. On Linux the parent notices the exit without reaping (``waitid`` with ``WNOWAIT``), so
the group's id stays reserved until the group has been killed, also after a normal exit. A
process the target starts that leaves the group on purpose (``setsid``) escapes that kill on
Linux; on Windows the job allows no breakaway.

Nothing calls this yet: the extraction stage's handler will be the first caller. The runner logs
nothing; the worker logs the job's outcome.
"""

from __future__ import annotations

import contextlib
import os
import pickle
import pickletools
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import IO, TYPE_CHECKING, Final, cast

from vault.jobs import sandbox_child as child

if TYPE_CHECKING:
    from vault.core.config import ProcessingSettings

MIB: Final = 1024 * 1024
MIN_MEMORY_BYTES: Final = 64 * MIB  # the child's interpreter alone takes most of this on Linux
DEFAULT_MAX_RESULT_BYTES: Final = 256 * MIB
# What the server may spend to unpickle one answer, and how many pickle opcodes it reads to judge
# that first (see _check_expansion): far above a real extraction, whose answer is a few large
# strings, and far below what a hostile answer of 256 MiB of one-byte opcodes would build.
DEFAULT_MAX_RESULT_MEMORY_BYTES: Final = 4 * DEFAULT_MAX_RESULT_BYTES
DEFAULT_MAX_RESULT_OPCODES: Final = 4_000_000
# Bytes the unpickler can allocate for one opcode besides the bytes of its argument, measured on
# CPython 3.12 with tracemalloc and rounded up: an empty set 241, a dict 89, a list 80 (the parent
# list's growth and a memo entry included), so 256 for the opcodes that make a set and 128 for any
# other opcode.
_SET_OPCODE_BYTES: Final = 256
_OPCODE_BYTES: Final = 128
_SET_OPCODES: Final = frozenset({"EMPTY_SET", "FROZENSET"})
# A mark is one slot in the unpickler's array of marks, which doubles as it grows
# (``load_mark`` in CPython's ``_pickle.c``): at most 16 bytes a mark once the array is grown.
_MARK_BYTES: Final = 16
# What one load costs before any opcode: the unpickler's own tables, measured at under 2 KiB.
_LOAD_BYTES: Final = 16 * 1024
# Opcodes that make nothing.
_FREE_OPCODES: Final = frozenset({"STOP", "PROTO", "FRAME", "POP", "POP_MARK"})
# The opcodes the child's pickler writes at ``child.PICKLE_PROTOCOL`` (5) for the values a result
# holds, and no others: an answer with any other opcode is refused before it is unpickled. This
# is what bounds the memo. Protocol 5 memoizes with ``MEMOIZE``, whose index is the memo's own
# length, so the table grows with the number of entries already charged; ``PUT``, ``BINPUT`` and
# ``LONG_BINPUT`` name an index of their own, and the unpickler grows its table to that index
# (``_Unpickler_MemoPut``: ``memo_size`` doubles past ``idx``), so nine bytes would allocate
# gigabytes. Also left out: the text opcodes of protocol 0 (each line is read once more as
# text), ``DUP``, the extension registry (``EXT1``, ``EXT2``, ``EXT4``), persistent ids and the
# out-of-band buffer opcodes, none of which the pickler writes for a result. A test pickles
# every kind of value a result holds and checks that its opcodes are in this set.
_RESULT_OPCODES: Final = frozenset(
    {
        "PROTO", "FRAME", "STOP", "MARK", "POP", "POP_MARK",
        "MEMOIZE", "BINGET", "LONG_BINGET",
        "NONE", "NEWTRUE", "NEWFALSE", "BININT", "BININT1", "BININT2", "LONG1", "LONG4",
        "BINFLOAT",
        "SHORT_BINBYTES", "BINBYTES", "BINBYTES8", "BYTEARRAY8",
        "SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8",
        "EMPTY_TUPLE", "TUPLE1", "TUPLE2", "TUPLE3", "TUPLE",
        "EMPTY_LIST", "APPEND", "APPENDS", "EMPTY_DICT", "SETITEM", "SETITEMS",
        "EMPTY_SET", "ADDITEMS", "FROZENSET",
        "STACK_GLOBAL", "REDUCE", "NEWOBJ", "NEWOBJ_EX", "BUILD",
    }
)  # fmt: skip
# Every kind of argument a pickle opcode has (``pickletools.ArgumentDescriptor.name``), and the
# types ``pickletools.genops`` decodes them to: numbers, strings, bytes and bytearrays, each
# charged its size as Python holds it. An opcode with an argument of any other kind, or one decoded
# to any other type, is refused, so an opcode a later Python adds cannot go uncharged; a test ties
# this list to ``pickletools.opcodes``.
_ARGUMENT_KINDS: Final = frozenset(
    {
        "uint1", "uint2", "int4", "uint4", "uint8", "long1", "long4",
        "decimalnl_short", "decimalnl_long", "floatnl", "float8",
        "string1", "string4", "stringnl", "stringnl_noescape", "stringnl_noescape_pair",
        "unicodestring1", "unicodestring4", "unicodestring8", "unicodestringnl",
        "bytes1", "bytes4", "bytes8", "bytearray8",
    }
)  # fmt: skip
_ARGUMENT_TYPES: Final = (int, float, str, bytes, bytearray)
RESULT_TYPE: Final = re.compile(r"vault(?:\.[a-z_][a-z0-9_]*)+:[A-Za-z_][A-Za-z0-9_]*")
SUPPORTED_PLATFORMS: Final = frozenset({"win32", "linux"})
# The variables a Python child needs to start and find its temporary folder; nothing else of the
# server's environment (API keys among it) is passed on.
CHILD_ENV: Final = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "HOME",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "COMSPEC",
        "PATHEXT",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    }
)
_POLL_SECONDS: Final = 0.01
_REAP_SECONDS: Final = 5.0
_READ_CHUNK: Final = 1 << 16


class SandboxError(Exception):
    """The sandboxed call did not return a result. Each subclass names one way it failed."""

    message = "the sandboxed call failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class SandboxRefusedError(SandboxError):
    message = "the entry point is not one the sandbox may run"


class SandboxUnavailableError(SandboxError):
    message = "the sandbox could not start or limit its child process on this system"


class SandboxTimeoutError(SandboxError):
    message = "the sandboxed call ran past its timeout and was killed"


class SandboxMemoryLimitError(SandboxError):
    message = "the sandboxed call ran out of memory at its cap"


class SandboxCrashError(SandboxError):
    message = "the sandboxed process ended without an answer"

    def __init__(self, exit_code: int | None) -> None:
        super().__init__()
        # Negative on Linux for a signal (-6 is SIGABRT), as subprocess reports it.
        self.exit_code = exit_code


class SandboxChildError(SandboxError):
    message = "the sandboxed call raised an exception"

    def __init__(self, error_class: str) -> None:
        super().__init__()
        # A class name only (``[A-Za-z_][A-Za-z0-9_]*``), never the exception's message.
        self.error_class = error_class


class SandboxResultError(SandboxError):
    message = "the sandboxed call's result could not be passed back"


@dataclass(frozen=True)
class EntryPoint:
    """A function the sandbox may run: ``vault.module:function``, top-level in its module.

    ``result_types`` lists the classes (``vault.module:Class``) its result may contain besides
    plain values; the unpickler resolves no other global name.
    """

    target: str
    result_types: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not child.TARGET.fullmatch(self.target):
            msg = "an entry point is a top-level function of a vault module (vault.x:function)"
            raise ValueError(msg)
        if not isinstance(self.result_types, frozenset) or not all(
            isinstance(name, str) and RESULT_TYPE.fullmatch(name) for name in self.result_types
        ):
            msg = "result types are classes of vault modules (vault.x:Class)"
            raise ValueError(msg)


# The extractors register their entry points here when the extraction stage is wired in.
ENTRY_POINTS: Final[Mapping[str, EntryPoint]] = MappingProxyType({})


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float
    memory_bytes: int
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES
    max_result_memory_bytes: int = DEFAULT_MAX_RESULT_MEMORY_BYTES
    max_result_opcodes: int = DEFAULT_MAX_RESULT_OPCODES

    def __post_init__(self) -> None:
        numbers = (
            self.timeout_seconds,
            self.memory_bytes,
            self.max_result_bytes,
            self.max_result_memory_bytes,
            self.max_result_opcodes,
        )
        if any(isinstance(n, bool) or not isinstance(n, int | float) for n in numbers):
            msg = "sandbox limits are numbers"
            raise TypeError(msg)
        if not all(isinstance(n, int) for n in numbers[1:]):
            msg = "sandbox byte and opcode limits are whole numbers"
            raise TypeError(msg)
        if not 0 < self.timeout_seconds < float("inf"):
            msg = "the sandbox timeout must be a positive, finite number of seconds"
            raise ValueError(msg)
        if self.memory_bytes < MIN_MEMORY_BYTES or self.max_result_bytes < 1:
            msg = f"the sandbox memory cap must be at least {MIN_MEMORY_BYTES // MIB} MiB"
            raise ValueError(msg)
        if self.max_result_memory_bytes < 1 or self.max_result_opcodes < 1:
            msg = "the limits on unpickling a result must be positive"
            raise ValueError(msg)

    @classmethod
    def from_settings(cls, settings: ProcessingSettings) -> SandboxLimits:
        """The limits in ``[processing]`` of ``config.toml``."""
        return cls(
            timeout_seconds=float(settings.sandbox_timeout_seconds),
            memory_bytes=settings.sandbox_memory_mib * MIB,
        )


class _ResultUnpickler(pickle.Unpickler):
    """Resolves only the classes an entry point lists; plain values need no global lookup."""

    def __init__(self, file: _ViewReader, allowed: frozenset[str]) -> None:
        super().__init__(file)
        self._allowed = allowed

    def find_class(self, module: str, name: str, /) -> object:
        if f"{module}:{name}" not in self._allowed:
            msg = "the result holds a type this entry point does not allow"
            raise pickle.UnpicklingError(msg)
        return super().find_class(module, name)


class _ViewReader:
    """The file interface the unpickler reads from, over a view: ``io.BytesIO`` would copy it."""

    def __init__(self, view: memoryview) -> None:
        self._view = view
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        end = len(self._view) if size < 0 else min(len(self._view), self._position + size)
        chunk = bytes(self._view[self._position : end])
        self._position = end
        return chunk

    def readinto(self, buffer: memoryview) -> int:
        target = memoryview(buffer).cast("B")
        chunk = self._view[self._position : self._position + len(target)]
        target[: len(chunk)] = chunk
        self._position += len(chunk)
        return len(chunk)

    def readline(self) -> bytes:
        """Up to and including the next newline (old pickle protocols have text lines)."""
        start = scan = self._position
        while scan < len(self._view):
            found = bytes(self._view[scan : scan + 4096]).find(b"\n")
            if found >= 0:
                return self.read(scan + found + 1 - start)
            scan += 4096
        return self.read()


def _check_expansion(payload: memoryview, max_memory: int, max_opcodes: int) -> int:
    """Refuse an answer whose unpickling would cost the server more than ``max_memory`` bytes.

    The answer's size is capped, but a pickle is a program that builds objects, and one byte can
    build an empty list of 80 bytes: 256 MiB of such opcodes would build gigabytes in the server,
    which the sandbox exists to protect. So the opcodes are read first, without building anything
    (``pickletools.genops``, over the same view, so the answer is not copied), and every
    allocation the unpickler makes from a value in the stream is bounded here:

    - only the opcodes in ``_RESULT_OPCODES`` are accepted, and the first must be ``PROTO`` 5,
      the protocol the child writes; so the memo grows only by ``MEMOIZE``, one entry per opcode
      charged, and never to an index the stream names;
    - each object an opcode makes is charged ``_OPCODE_BYTES`` (``_SET_OPCODE_BYTES`` for a set),
      which covers the growth of the list, dict or memo it goes into, plus its decoded argument
      as Python holds it (an argument of a kind ``_ARGUMENT_KINDS`` does not list is refused);
    - each ``MARK`` is charged its slot in the array of marks (``_MARK_BYTES``);
    - the load is charged ``_LOAD_BYTES`` and the answer's length once more, for the bytes the
      unpickler reads out of the view before decoding a string or a frame.

    A length a ``BINBYTES``, ``BINUNICODE`` or ``FRAME`` opcode declares is never allocated
    unread: genops reads the data whole, so a length longer than the answer is refused here, and
    the unpickler's reader copies only bytes that exist. The read stops at the first opcode past
    ``max_opcodes`` or the first byte past ``max_memory``, so judging a hostile answer costs a
    bounded time too. Objects the unpickler would share (memo lookups) are charged as if new,
    which only errs on the safe side. No global is resolved here; the unpickler still checks
    every class it is asked for. Returns the bytes charged."""
    charged = _LOAD_BYTES + len(payload)
    if charged > max_memory:
        raise SandboxResultError
    for count, (opcode, argument, _position) in enumerate(
        # genops calls only read and readline (and tell, which the reader has not got).
        pickletools.genops(cast("IO[bytes]", _ViewReader(payload))),
        start=1,
    ):
        if count > max_opcodes or opcode.name not in _RESULT_OPCODES:
            raise SandboxResultError
        if count == 1 and (opcode.name != "PROTO" or argument != child.PICKLE_PROTOCOL):
            raise SandboxResultError
        if opcode.name in _FREE_OPCODES:
            continue
        if opcode.name == "MARK":
            charged += _MARK_BYTES
        elif opcode.name in _SET_OPCODES:
            charged += _SET_OPCODE_BYTES
        else:
            charged += _OPCODE_BYTES
        if opcode.arg is not None:
            if opcode.arg.name not in _ARGUMENT_KINDS or not isinstance(argument, _ARGUMENT_TYPES):
                raise SandboxResultError
            charged += sys.getsizeof(argument)
        if charged > max_memory:
            raise SandboxResultError
    return charged


def _load_result(
    payload: memoryview,
    allowed: frozenset[str],
    max_memory: int = DEFAULT_MAX_RESULT_MEMORY_BYTES,
    max_opcodes: int = DEFAULT_MAX_RESULT_OPCODES,
) -> object:
    """Unpickle the result, once :func:`_check_expansion` has shown that building it fits the
    server's budget. A failure, an answer over the budget or running out of the server's memory
    included, is a :class:`SandboxResultError`; the answer itself is already bounded by
    ``max_result_bytes``."""
    try:
        _check_expansion(payload, max_memory, max_opcodes)
        return _ResultUnpickler(_ViewReader(payload), allowed).load()
    except Exception:
        raise SandboxResultError from None


def child_env() -> dict[str, str]:
    """The environment the child starts with: :data:`CHILD_ENV` only."""
    return {key: value for key, value in os.environ.items() if key.upper() in CHILD_ENV}


def run(
    entry: str,
    data: bytes | bytearray | memoryview,
    limits: SandboxLimits,
    *,
    entry_points: Mapping[str, EntryPoint] = ENTRY_POINTS,
) -> object:
    """Run the entry point named ``entry`` on ``data`` in a child process and return its result.

    Raises a :class:`SandboxError` subclass when there is no result. Safe to call from several
    threads at once: each call has its own child (and on Windows its own job).
    """
    point = entry_points.get(entry) if isinstance(entry, str) else None
    if not isinstance(point, EntryPoint):
        raise SandboxRefusedError
    if not isinstance(data, bytes | bytearray | memoryview):
        msg = "the sandbox input is bytes"
        raise TypeError(msg)
    if not isinstance(limits, SandboxLimits):
        msg = "limits must be SandboxLimits"
        raise TypeError(msg)
    if sys.platform not in SUPPORTED_PLATFORMS:
        raise SandboxUnavailableError
    # A view, not a copy: the input can be hundreds of MiB.
    view = memoryview(data).cast("B")
    answer, exit_code, timed_out, overflow = _Child(point.target, limits).run(view)
    if timed_out:
        raise SandboxTimeoutError
    if overflow:
        raise SandboxResultError
    if exit_code != 0:
        raise SandboxCrashError(exit_code)
    return _interpret(
        answer,
        point.result_types,
        max_memory=limits.max_result_memory_bytes,
        max_opcodes=limits.max_result_opcodes,
    )


def _interpret(
    answer: bytes | bytearray,
    allowed: frozenset[str],
    *,
    max_memory: int = DEFAULT_MAX_RESULT_MEMORY_BYTES,
    max_opcodes: int = DEFAULT_MAX_RESULT_OPCODES,
) -> object:
    # A view of the payload, so a result near the cap is not copied again before unpickling.
    status, payload = bytes(answer[:1]), memoryview(answer)[1:]
    if status == child.OK:
        return _load_result(payload, allowed, max_memory, max_opcodes)
    if status == child.ERROR:
        name = bytes(payload[:101]).decode("ascii", errors="replace")
        raise SandboxChildError(name if child.ERROR_NAME.fullmatch(name) else child.UNNAMED_ERROR)
    if payload:
        raise SandboxResultError
    if status == child.MEMORY:
        raise SandboxMemoryLimitError
    if status == child.UNPICKLABLE:
        raise SandboxResultError
    if status == child.REFUSED:
        raise SandboxRefusedError
    if status == child.SETUP:
        raise SandboxUnavailableError
    if status == b"":
        raise SandboxCrashError(0)
    raise SandboxResultError


class _Child:
    """One child process: start, limit, feed, wait, read, and always kill and reap."""

    def __init__(self, target: str, limits: SandboxLimits) -> None:
        self._target = target
        self._limits = limits
        self._answer = bytearray()
        self._overflow = threading.Event()
        self._input_bytes = 0

    def run(self, data: memoryview) -> tuple[bytearray, int | None, bool, bool]:
        """Returns the answer, the exit code, whether it timed out and whether it overflowed."""
        self._input_bytes = data.nbytes
        deadline = time.monotonic() + self._limits.timeout_seconds
        guard = _Guard(self._limits.memory_bytes)
        try:
            process = self._start()
        except OSError:
            guard.close()
            raise SandboxUnavailableError from None
        pumps: list[threading.Thread] = []
        timed_out = False
        try:
            if not guard.attach(process):
                raise SandboxUnavailableError
            pumps = [
                threading.Thread(target=_feed, args=(process.stdin, data), daemon=True),
                threading.Thread(target=self._drain, args=(process.stdout,), daemon=True),
            ]
            for pump in pumps:
                pump.start()
            while not guard.exited(process):
                if self._overflow.is_set():
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                self._overflow.wait(_POLL_SECONDS)
        finally:
            # Kill the group or job even after a normal exit, so nothing the child started
            # outlives it, then reap the child and let the pumps see the pipes close.
            guard.kill(process)
            exit_code: int | None
            try:
                exit_code = process.wait(_REAP_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - a process that ignores SIGKILL
                exit_code = None
            for pump in pumps:
                pump.join(_REAP_SECONDS)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            guard.close()
        return self._answer, exit_code, timed_out, self._overflow.is_set()

    def _start(self) -> subprocess.Popen[bytes]:
        command = [
            sys.executable,
            "-I",
            "-m",
            child.__name__,
            self._target,
            str(self._limits.memory_bytes),
            str(os.getpid()),
            str(self._input_bytes),
        ]
        if sys.platform == "win32":  # pragma: no cover - Windows only
            # Suspended until it is in the job: in a virtual environment ``sys.executable`` is a
            # launcher that starts the real interpreter as its own child, and a process started
            # before its parent joined a job would stay outside it.
            create_suspended, create_no_window = 0x00000004, 0x08000000
            return subprocess.Popen(  # noqa: S603 - this interpreter and a fixed module
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=child_env(),
                creationflags=create_suspended | create_no_window,
            )
        return subprocess.Popen(  # noqa: S603 - this interpreter and a fixed module
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env(),
            start_new_session=True,
        )

    def _drain(self, stream: IO[bytes] | None) -> None:
        """Read the answer up to the cap (plus the status byte); past it, read and drop."""
        if stream is None:  # pragma: no cover - always a pipe
            return
        limit = self._limits.max_result_bytes + 1
        try:
            while chunk := stream.read1(_READ_CHUNK):  # type: ignore[attr-defined]
                if self._overflow.is_set():
                    continue
                if len(self._answer) + len(chunk) > limit:
                    self._overflow.set()
                    self._answer.clear()
                    continue
                self._answer += chunk
        except (OSError, ValueError):  # the pipe was closed under us at teardown
            pass


def _feed(stream: IO[bytes] | None, data: memoryview) -> None:
    """Write the input and close the pipe, so the child sees its end. A child that stops reading
    (it crashed, or never reads) breaks the pipe; that is its outcome to report, not ours."""
    if stream is None:  # pragma: no cover - always a pipe
        return
    try:
        stream.write(data)
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError, ValueError):
            stream.close()


if sys.platform == "win32":  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x0200
    _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _TH32CS_SNAPTHREAD = 0x0004
    _THREAD_SUSPEND_RESUME = 0x0002
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _RESUME_FAILED = 0xFFFFFFFF

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD

    def _resume(pid: int) -> bool:
        """Resume the threads of a process started with ``CREATE_SUSPENDED`` (it has one).

        ``subprocess`` closes the thread handle that ``CreateProcess`` returns, so the thread is
        found again through a Toolhelp snapshot. True if at least one thread was resumed.
        """
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _INVALID_HANDLE:
            return False
        resumed = 0
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(_ThreadEntry)
            more = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = _kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if thread:
                        try:
                            if _kernel32.ResumeThread(thread) != _RESUME_FAILED:
                                resumed += 1
                        finally:
                            _kernel32.CloseHandle(thread)
                more = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
        return resumed > 0

    def _new_job(flags: int, memory_bytes: int = 0) -> int:
        """A new Job Object with these limit flags, or :class:`SandboxUnavailableError`."""
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise SandboxUnavailableError
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = flags
        info.ProcessMemoryLimit = memory_bytes
        info.JobMemoryLimit = memory_bytes
        if not _kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _kernel32.CloseHandle(handle)
            raise SandboxUnavailableError
        return int(handle)

    _server_job_lock = threading.Lock()
    _server_job_handle: int | None = None

    def server_job() -> int:
        """A kill-on-close job that holds this process, made on the first run.

        A process started by a member of a job is in that job from the moment it is created, so
        each child is contained before ``Popen`` even returns: if the server dies in the window
        before the child is in its own job (while it is still suspended), Windows closes this
        handle, which only the server holds, and ends the child. ``subprocess`` cannot create a
        process directly in a job (``PROC_THREAD_ATTRIBUTE_JOB_LIST``: its attribute list takes
        only a handle list), so the server's own membership gives the same atomicity. The
        per-run job then nests inside this one (Windows 8 and later).
        """
        global _server_job_handle
        with _server_job_lock:
            if _server_job_handle is None:
                job = _new_job(_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
                if not _kernel32.AssignProcessToJobObject(job, _kernel32.GetCurrentProcess()):
                    _kernel32.CloseHandle(job)
                    raise SandboxUnavailableError
                _server_job_handle = job
            return _server_job_handle

    class _Guard:
        """A Job Object with a memory limit and kill-on-close, holding the child."""

        def __init__(self, memory_bytes: int) -> None:
            server_job()  # before the child exists, so it is born inside the server's job
            self._job: int | None = _new_job(
                _JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | _JOB_OBJECT_LIMIT_JOB_MEMORY
                | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
                | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                memory_bytes,
            )

        def attach(self, process: subprocess.Popen[bytes]) -> bool:
            # The Popen object holds the process handle until it is reaped, so the id cannot
            # name another process here.
            handle = _kernel32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid
            )
            if not handle:
                return False
            try:
                if not _kernel32.AssignProcessToJobObject(self._job, handle):
                    return False
            finally:
                _kernel32.CloseHandle(handle)
            return _resume(process.pid)

        def exited(self, process: subprocess.Popen[bytes]) -> bool:
            return process.poll() is not None

        def kill(self, process: subprocess.Popen[bytes]) -> None:
            # The job holds the child and everything it started. The child itself is also ended
            # directly, because it is outside the job when attaching failed (and then still
            # suspended, which closing the job would not end).
            if self._job is not None:
                _kernel32.TerminateJobObject(self._job, 1)
            if process.poll() is None:
                with contextlib.suppress(OSError):
                    process.kill()

        def close(self) -> None:
            if self._job is not None:
                _kernel32.CloseHandle(self._job)
                self._job = None

else:

    class _Guard:
        """The child's process group (it is a session leader). The child limits its own memory."""

        def __init__(self, memory_bytes: int) -> None:
            self._memory_bytes = memory_bytes

        def attach(self, process: subprocess.Popen[bytes]) -> bool:
            return True

        def exited(self, process: subprocess.Popen[bytes]) -> bool:
            """True once the child has ended, without reaping it: until it is reaped, its id
            (and so its group's id) cannot be given to another process."""
            try:
                return (
                    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    is not None
                )
            except ChildProcessError:  # pragma: no cover - already reaped
                return True

        def kill(self, process: subprocess.Popen[bytes]) -> None:
            if process.returncode is not None:  # pragma: no cover - reaped already: the id is free
                return
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)

        def close(self) -> None:
            pass
