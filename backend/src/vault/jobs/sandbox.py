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
- :class:`SandboxResultError`: the result could not be encoded, was larger than the cap, did not
  have the type this entry point declares, or was not a well-formed answer;
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
child gets a reduced environment (:data:`CHILD_ENV`), without API keys or other secrets.

**The result is JSON, never a pickle.** A child that a hostile file took over controls every byte of
its answer, so the server decodes nothing that can run code or name a type: the child writes the
result as ASCII JSON (:func:`~vault.jobs.sandbox_child.encode`: ``None``, booleans, integers, finite
floats, strings, bytes as ``{"$bytes": "<base64>"}``, lists and tuples as arrays, and dataclass
instances as objects of their fields), and the server parses it with the standard library's ``json``
and then builds the value the entry point declares (:attr:`EntryPoint.result_type`, a type checked
when the entry point is defined) with an explicit converter that accepts only that type's shape: a
declared dataclass is built from an object with exactly its field names, and nothing in the answer
chooses a class or a function. Before parsing, the server bounds what the answer can cost, from its
bytes alone: every JSON value but the outermost follows a ``[``, ``{``, ``,`` or ``:``, so their
count bounds the values ``json`` builds; an answer that could cost more than
``max_result_memory_bytes`` (1 GiB by default) or hold more than ``max_result_values`` (4 million)
values is refused unparsed, because 256 MiB of ``[],`` would otherwise build about 6 GiB in the
server. Nesting is bounded by :data:`~vault.jobs.sandbox_child.MAX_DEPTH`, ``NaN``, ``Infinity``
and repeated keys are refused, and a number is read only if its literal is exactly the text the
child writes for the value it reads back (:func:`~vault.jobs.sandbox_child.read_number`, the one
rule for numbers on both sides), so ``1e999`` (infinity), ``1e-400`` (zero) and other spellings
are refused.

**Memory cap.** On Windows the child is put in a Job Object with a process and a job memory limit
(committed memory) and kill-on-close: when the server process ends for any reason, Windows closes
the job's handle and ends every process in it, so a crash of the server leaves no orphan. The child
starts suspended and runs only once it is in the job: in a virtual environment ``sys.executable`` is
a launcher that starts the real interpreter as its own child, and a process started before its
parent joined the job would stay outside it. Processes started inside the job are in it too, so the
cap and the kill cover the real interpreter and anything it starts. Before the first child, the
server puts itself in a kill-on-close job of its own (:func:`server_job`), so every child is in that
job from the moment it is created: a server that dies while a child is still suspended and outside
its own job takes the child with it. ``subprocess`` cannot create a process directly in a job
(``PROC_THREAD_ATTRIBUTE_JOB_LIST``), which would be the other way to close that window. On Linux
the child caps its own address space with ``RLIMIT_AS`` before it reads the input, and asks the
kernel to kill it when the parent dies (``PR_SET_PDEATHSIG``), then checks that its parent is still
the server, so a server that died before that call is caught too; until the child has its input it
waits on a pipe that the server's death closes. ``RLIMIT_AS`` caps one process, and a process it
started would get a cap of its own (and no death signal), so the child then installs a seccomp
filter that refuses to start another process (``fork``, ``vfork``, and ``clone`` without
``CLONE_THREAD``; threads still start): on Linux the target runs as one process, so the cap is the
total, as the job makes it on Windows, and a target that starts a program fails (with
``PermissionError``) instead of escaping the cap. A kernel or an architecture where the filter
cannot be installed (other than x86-64 and ARM64) is :class:`SandboxUnavailableError`. Address space
is larger than committed memory (libraries reserve more than they use), so the same number is
stricter on Linux; the Linux path is for development and CI, the product runs on Windows. An
allocation that fails raises ``MemoryError`` in the child on both systems, and the child reports it
as :class:`SandboxMemoryLimitError` at every step that grows with the input or the result: reading
the input (into one ``bytes`` of the length given on its command line, so it is held once),
importing and running the target, encoding the result and writing the answer. A library that aborts
instead shows up as :class:`SandboxCrashError`. The parent passes the input as a view and reads the
answer into one buffer bounded by ``max_result_bytes``, decoding its text from a view of it, so the
buffer is not copied first. Other systems are not supported (:class:`SandboxUnavailableError`).

**Timeout.** The timeout covers the whole run, the child's start included. On expiry the parent
ends the child's whole process group (Linux, ``start_new_session``) or job (Windows), then reaps
the child. On Linux the parent notices the exit without reaping (``waitid`` with ``WNOWAIT``), so
the group's id stays reserved until the group has been killed, also after a normal exit. On
Linux the target cannot start a process that could leave the group; on Windows the job allows
no breakaway.

Nothing calls this yet: the extraction stage's handler will be the first caller. The runner logs
nothing; the worker logs the job's outcome.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import dataclasses
import inspect
import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import IO, TYPE_CHECKING, Final

from vault.jobs import sandbox_child as child

if TYPE_CHECKING:
    from vault.core.config import ProcessingSettings

MIB: Final = 1024 * 1024
MIN_MEMORY_BYTES: Final = 64 * MIB  # the child's interpreter alone takes most of this on Linux
DEFAULT_MAX_RESULT_BYTES: Final = 256 * MIB
# What the server may spend to decode one answer, and how many JSON values the answer may hold
# (see _answer_cost): far above a real extraction, whose answer is a few large strings, and far
# below what a hostile answer of 256 MiB of ``[],`` would build (about 6 GiB).
DEFAULT_MAX_RESULT_MEMORY_BYTES: Final = 4 * DEFAULT_MAX_RESULT_BYTES
DEFAULT_MAX_RESULT_VALUES: Final = 4_000_000
# What decoding can allocate for each byte of the answer: the answer as text (1), the strings json
# builds from it (up to 4: one escaped character outside the Basic Multilingual Plane makes every
# character of its string four bytes wide) and the bytes decoded from base64 together with the
# ASCII copy ``b64decode`` makes of its string first (2).
_ANSWER_BYTE_COST: Final = 7
# What decoding can allocate for each JSON value besides the bytes of its text: the object json
# builds (an empty list 56 bytes, a one-key dict 184), its slot in the list or dict around it, the
# scanner's memo entry for a key, the list of pairs ``object_pairs_hook`` receives, and the object
# the converter builds from it (a tuple, a dataclass instance). Measured with tracemalloc on
# CPython 3.12 at under 110 bytes for every shape and rounded up; tests compare it with the real
# peak for each shape.
_VALUE_COST: Final = 256
# What one decode costs before any value: the decoder's own objects.
_LOAD_BYTES: Final = 16 * 1024
# Every JSON value but the outermost follows one of these bytes (an array's first item, an
# object's first key, every later item or key, every key's value), also in an answer that is not
# valid JSON, as far as json reads it; so their count plus one bounds the values json builds. The
# same bytes inside strings are counted too, which only errs on the safe side.
_VALUE_MARKS: Final = (b"[", b"{", b",", b":")
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


class _MismatchError(Exception):
    """The answer does not have the declared type's shape."""


class _Node:
    """One part of a declared result type: builds that part's value from what json parsed."""

    def build(self, value: object, depth: int) -> object:
        raise NotImplementedError


def _enter(depth: int) -> int:
    """The depth of a container's items; refuses past ``MAX_DEPTH``, as the child does."""
    if depth >= child.MAX_DEPTH:
        raise _MismatchError
    return depth + 1


class _Exact(_Node):
    """``None``, ``bool``, ``int``, ``float`` or ``str``: json built it; only its type is checked
    (exactly, so ``True`` is not an ``int`` and ``1`` is not a ``float``)."""

    def __init__(self, kind: type) -> None:
        self.kind = kind

    def build(self, value: object, depth: int) -> object:
        if type(value) is not self.kind:
            raise _MismatchError
        return value


class _Bytes(_Node):
    """``bytes``: an object whose only key is ``$bytes``, holding strict base64."""

    def build(self, value: object, depth: int) -> object:
        if type(value) is not dict or len(value) != 1:
            raise _MismatchError
        text = value.get(child.BYTES_KEY)
        if type(text) is not str:
            raise _MismatchError
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise _MismatchError from None


class _Sequence(_Node):
    """``list[X]`` or ``tuple[X, ...]``: an array whose every item is an ``X``."""

    def __init__(self, item: _Node, *, as_tuple: bool) -> None:
        self.item = item
        self.as_tuple = as_tuple

    def build(self, value: object, depth: int) -> object:
        if type(value) is not list:
            raise _MismatchError
        inner = _enter(depth)
        items = [self.item.build(item, inner) for item in value]
        return tuple(items) if self.as_tuple else items


class _Tuple(_Node):
    """``tuple[X, Y]``: an array of exactly that many items, each of its own type."""

    def __init__(self, items: tuple[_Node, ...]) -> None:
        self.items = items

    def build(self, value: object, depth: int) -> object:
        if type(value) is not list or len(value) != len(self.items):
            raise _MismatchError
        inner = _enter(depth)
        return tuple(node.build(item, inner) for node, item in zip(self.items, value, strict=True))


class _Optional(_Node):
    """``X | None``: ``null``, or an ``X``."""

    def __init__(self, inner: _Node) -> None:
        self.inner = inner

    def build(self, value: object, depth: int) -> object:
        return None if value is None else self.inner.build(value, depth)


class _Dataclass(_Node):
    """A declared dataclass: an object with exactly its field names, each of its field's type.

    The class is the one the entry point declares, never one the answer names; it is built by
    calling its generated ``__init__`` with the fields as keywords (:func:`_round_trips` refused
    any class whose own code could run there).
    """

    def __init__(self, cls: type) -> None:
        self.cls = cls
        self.fields: dict[str, _Node] = {}  # filled after creation, so a class can hold itself

    def build(self, value: object, depth: int) -> object:
        if type(value) is not dict or value.keys() != self.fields.keys():
            raise _MismatchError
        inner = _enter(depth)
        built = {name: node.build(value[name], inner) for name, node in self.fields.items()}
        return self.cls(**built)


_RESULT_TYPE_RULE: Final = (
    "a result type is None, bool, int, float, str, bytes, list[X], tuple[X, ...], tuple[X, Y], "
    "X | None, or a plain dataclass whose fields are all initialised and of these types"
)


def _generated(function: object) -> bool:
    """True for a method ``dataclasses`` wrote (CPython 3.12 compiles each in ``__create_fn__``).

    Should a later Python name it otherwise, every dataclass is refused, never one let through.
    """
    code = getattr(function, "__code__", None)
    return isinstance(code, types.CodeType) and code.co_qualname.startswith("__create_fn__.")


def _round_trips(cls: type) -> bool:
    """True when the converter's call ``cls(**fields)`` rebuilds exactly what the child wrote
    from ``dataclasses.fields`` of an instance, with nothing of the class's own code in between.

    Refused: a field with ``init=False`` (the child writes it, ``__init__`` does not take it), an
    ``InitVar`` (``__init__`` requires it, the child cannot write it), an ``__init__`` the class
    or a base wrote itself (``dataclass`` keeps it, and it may take other arguments or change the
    values), a ``__post_init__`` (it may change what ``__init__`` stored), a ``__new__``,
    ``__setattr__`` (other than a frozen dataclass's own), ``__getattribute__`` or ``__getattr__``
    of the class's own, a metaclass, and a field whose class attribute is a descriptor (it would
    run on every read and write). A ``ClassVar`` is no field, so it is neither written nor read,
    and ``kw_only`` fields are passed by name: both are accepted.
    """
    fields = dataclasses.fields(cls)
    if type(cls) is not type or any(not item.init for item in fields):
        return False
    init = cls.__dict__.get("__init__")
    if init is None or not _generated(init):
        return False
    try:
        parameters = list(inspect.signature(init).parameters.values())[1:]
    except (TypeError, ValueError):
        return False
    kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    if [parameter.name for parameter in parameters] != [item.name for item in fields] or any(
        parameter.kind not in kinds for parameter in parameters
    ):
        return False
    if getattr(cls, "__post_init__", None) is not None or getattr(cls, "__getattr__", None):
        return False
    new, get, set_ = (getattr(cls, name) for name in ("__new__", "__getattribute__", "__setattr__"))
    if new is not object.__new__ or get is not object.__getattribute__:
        return False
    if set_ is not object.__setattr__ and not _generated(set_):
        return False
    for item in fields:
        attribute = inspect.getattr_static(cls, item.name, None)
        if hasattr(type(attribute), "__get__") and not isinstance(
            attribute, types.MemberDescriptorType
        ):
            return False
    return True


def _compile_result_type(form: object, classes: dict[type, _Dataclass] | None = None) -> _Node:
    """The converter for a declared result type, or ``ValueError`` if it is not one the sandbox
    can bring back. Only the forms in ``_RESULT_TYPE_RULE`` are accepted: no ``dict``, ``set``,
    ``Any``, ``object``, other unions, a class that is not a dataclass, a dataclass instance, a
    function or a name in a string."""
    classes = {} if classes is None else classes
    if form is None or form is type(None):
        return _Exact(type(None))
    if any(form is kind for kind in (bool, int, float, str)):
        return _Exact(typing.cast("type", form))
    if form is bytes:
        return _Bytes()
    origin, args = typing.get_origin(form), typing.get_args(form)
    if origin is list and len(args) == 1:
        return _Sequence(_compile_result_type(args[0], classes), as_tuple=False)
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        return _Sequence(_compile_result_type(args[0], classes), as_tuple=True)
    if origin is tuple and all(arg is not Ellipsis for arg in args):
        return _Tuple(tuple(_compile_result_type(arg, classes) for arg in args))
    if origin in (types.UnionType, typing.Union) and len(args) == 2 and type(None) in args:
        (other,) = (arg for arg in args if arg is not type(None))
        return _Optional(_compile_result_type(other, classes))
    if origin is None and isinstance(form, type) and dataclasses.is_dataclass(form):
        if form in classes:
            return classes[form]
        if not _round_trips(form):
            raise ValueError(_RESULT_TYPE_RULE)
        node = classes[form] = _Dataclass(form)
        try:
            hints = typing.get_type_hints(form)
        except Exception:
            raise ValueError(_RESULT_TYPE_RULE) from None
        for item in dataclasses.fields(form):
            node.fields[item.name] = _compile_result_type(hints[item.name], classes)
        return node
    raise ValueError(_RESULT_TYPE_RULE)


@dataclass(frozen=True)
class EntryPoint:
    """A function the sandbox may run: ``vault.module:function``, top-level in its module.

    ``result_type`` is the type the function returns, as a type object (``str``, ``list[Page]``,
    a dataclass): checked here, when the entry point is defined, against ``_RESULT_TYPE_RULE``.
    The server builds only that type from the child's answer.
    """

    target: str
    result_type: object
    _decoder: _Node = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not child.TARGET.fullmatch(self.target):
            msg = "an entry point is a top-level function of a vault module (vault.x:function)"
            raise ValueError(msg)
        object.__setattr__(self, "_decoder", _compile_result_type(self.result_type))


# The extractors register their entry points here when the extraction stage is wired in.
ENTRY_POINTS: Final[Mapping[str, EntryPoint]] = MappingProxyType({})


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float
    memory_bytes: int
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES
    max_result_memory_bytes: int = DEFAULT_MAX_RESULT_MEMORY_BYTES
    max_result_values: int = DEFAULT_MAX_RESULT_VALUES

    def __post_init__(self) -> None:
        numbers = (
            self.timeout_seconds,
            self.memory_bytes,
            self.max_result_bytes,
            self.max_result_memory_bytes,
            self.max_result_values,
        )
        if any(isinstance(n, bool) or not isinstance(n, int | float) for n in numbers):
            msg = "sandbox limits are numbers"
            raise TypeError(msg)
        if not all(isinstance(n, int) for n in numbers[1:]):
            msg = "sandbox byte and value limits are whole numbers"
            raise TypeError(msg)
        if not 0 < self.timeout_seconds < float("inf"):
            msg = "the sandbox timeout must be a positive, finite number of seconds"
            raise ValueError(msg)
        if self.memory_bytes < MIN_MEMORY_BYTES or self.max_result_bytes < 1:
            msg = f"the sandbox memory cap must be at least {MIN_MEMORY_BYTES // MIB} MiB"
            raise ValueError(msg)
        if self.max_result_memory_bytes < 1 or self.max_result_values < 1:
            msg = "the limits on decoding a result must be positive"
            raise ValueError(msg)

    @classmethod
    def from_settings(cls, settings: ProcessingSettings) -> SandboxLimits:
        """The limits in ``[processing]`` of ``config.toml``."""
        return cls(
            timeout_seconds=float(settings.sandbox_timeout_seconds),
            memory_bytes=settings.sandbox_memory_mib * MIB,
        )


def _answer_cost(answer: bytes | bytearray) -> tuple[int, int]:
    """What decoding the answer's payload (after its status byte) can cost the server at most, in
    bytes, and how many JSON values it can hold at most, read from its bytes without parsing them.

    ``bytes.count`` scans the buffer in place, so judging a hostile answer of the largest size
    allocates nothing and takes a fraction of a second.
    """
    values = 1 + sum(answer.count(mark, 1) for mark in _VALUE_MARKS)
    length = max(len(answer) - 1, 0)
    return _LOAD_BYTES + _ANSWER_BYTE_COST * length + _VALUE_COST * values, values


def _parse_int(text: str) -> object:
    """An integer literal, read by the child's own rule for numbers (``read_number``)."""
    try:
        return child.read_number(text, int)
    except ValueError:
        raise _MismatchError from None


def _parse_float(text: str) -> object:
    """A literal with a fraction or an exponent, read by the same rule: ``1e999`` is refused."""
    try:
        return child.read_number(text, float)
    except ValueError:
        raise _MismatchError from None


def _refuse_constant(text: str) -> object:
    raise _MismatchError  # NaN, Infinity, -Infinity: the child refuses to write them


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A JSON object as a dict, refused if a key repeats (json would keep the last silently)."""
    built = dict(pairs)
    if len(built) != len(pairs):
        raise _MismatchError
    return built


def _load_result(
    answer: bytes | bytearray,
    decoder: _Node,
    max_memory: int = DEFAULT_MAX_RESULT_MEMORY_BYTES,
    max_values: int = DEFAULT_MAX_RESULT_VALUES,
) -> object:
    """Decode the payload after the status byte into the declared result type.

    Refused unparsed when :func:`_answer_cost` says it could cost more than ``max_memory`` bytes
    or hold more than ``max_values`` values. Then the payload is read as ASCII (the child escapes
    everything else) from a view of the buffer, parsed by the standard library's ``json`` with
    ``NaN``, ``Infinity``, repeated keys and every number literal that is not exactly the text the
    child writes for its value (``sandbox_child.read_number``: no overflow, underflow, precision
    loss, other spelling or literal longer than the child writes) refused,
    and built by the entry point's converter, which accepts only the declared type's shape and
    nesting up to ``MAX_DEPTH``. json's own nesting is bounded by CPython's recursion limit, which
    raises ``RecursionError``. Any failure, running out of the server's memory included, is a
    :class:`SandboxResultError`; the answer itself is already bounded by ``max_result_bytes``.
    """
    try:
        cost, values = _answer_cost(answer)
        if cost > max_memory or values > max_values:
            raise SandboxResultError
        text = str(memoryview(answer)[1:], "ascii")
        parsed = json.loads(
            text,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_refuse_constant,
            object_pairs_hook=_object,
        )
        del text
        return decoder.build(parsed, 0)
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
        point._decoder,
        max_memory=limits.max_result_memory_bytes,
        max_values=limits.max_result_values,
    )


def _interpret(
    answer: bytes | bytearray,
    decoder: _Node,
    *,
    max_memory: int = DEFAULT_MAX_RESULT_MEMORY_BYTES,
    max_values: int = DEFAULT_MAX_RESULT_VALUES,
) -> object:
    status, payload = bytes(answer[:1]), memoryview(answer)[1:]
    if status == child.OK:
        return _load_result(answer, decoder, max_memory, max_values)
    if status == child.ERROR:
        name = bytes(payload[:101]).decode("ascii", errors="replace")
        raise SandboxChildError(name if child.ERROR_NAME.fullmatch(name) else child.UNNAMED_ERROR)
    if payload:
        raise SandboxResultError
    if status == child.MEMORY:
        raise SandboxMemoryLimitError
    if status == child.UNENCODABLE:
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
