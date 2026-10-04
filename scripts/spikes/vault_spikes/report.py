"""Spike reports: metrics only, never document text.

A report is built from names fixed in code (a spike, a candidate, a metric) and numbers. Its
title, question and method are fixed text per spike, and the only other strings (the machine and
package descriptions) are checked against a fixed vocabulary and strict patterns, so a script cannot
put document text, a file name or an OCR result into a report by accident: anything else is a
TypeError or ValueError. A report read back from a JSON file is checked the same way, its title,
question and method must equal the fixed text of the spike it names, and every candidate, metric and
package name must be one that spike can produce, not merely one of the right shape. The Markdown
output has the five sections that ``scripts/check_adrs.py`` requires of a spike file, with the
conclusion left for the owner's review.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import tempfile
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Final, NamedTuple, cast

from vault_spikes.errors import OwnTypeError, OwnValueError

# Always use ``fullmatch`` with these patterns (``$`` also matches before a trailing newline).
SPIKE_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
CANDIDATE = re.compile(r"[a-z0-9][a-z0-9._+-]{0,60}")
METRIC = re.compile(r"[a-z][a-z0-9_]{0,48}")
# A machine or GPU description such as "AMD64 Family 25 Model 97, AuthenticAMD" is built from a
# closed vocabulary of hardware and operating system words plus model numbers (``7800X3D``,
# ``i7-12700K``, ``3080``), at most 100 characters. A pattern over letters cannot tell a CPU name
# from a sentence (``Patient diagnosis confidential`` fits "letters and spaces"), so a word the
# vocabulary does not know is refused when a report is built, merged or read back, and the code that
# reads the machine stores ``unknown`` instead. ``MACHINE_CHARACTERS`` is only the first, cheap
# filter; ``is_machine_text`` is the rule.
MACHINE_CHARACTERS = re.compile(r"[A-Za-z0-9 ._+()/,:-]{1,100}")
_MACHINE_SEPARATORS = re.compile(r"[ ._+()/,:-]+")
_MODEL_NUMBER = re.compile(r"[a-z]{0,2}\d{1,6}[a-z]{0,2}\d{0,2}[a-z]{0,2}")
MACHINE_WORDS: Final = frozenset(
    {
        # operating systems and architectures
        "windows", "linux", "darwin", "macos", "sp", "x86", "arm", "arm64", "aarch64", "amd64",
        "intel64", "i386", "i686", "x64", "ia64", "riscv64", "ppc64le", "s390x", "unknown",
        # processors
        "amd", "authenticamd", "intel", "genuineintel", "ryzen", "threadripper", "epyc", "athlon",
        "core", "xeon", "pentium", "celeron", "atom", "apple", "qualcomm", "snapdragon", "family",
        "model", "stepping", "processor", "cpu", "r", "tm", "ultra", "pro", "max", "mobile",
        "series", "with", "ai", "graphics",
        # graphics cards
        "nvidia", "geforce", "rtx", "gtx", "gt", "quadro", "tesla", "titan", "radeon", "rx",
        "arc", "ti", "super", "xt", "xtx", "laptop", "gpu", "mib", "gib", "q",
    }
)  # fmt: skip
PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}")
# A version is numbers with at most a pre-release, post-release, dev and local part from a closed
# list (PEP 440 shapes such as ``2.8.0+cu128`` or ``1.0.0rc1``). A word in the version position is
# free text (``patient-diagnosis-confidential``), so the only words allowed are the two sentinels
# and the fixed local labels of the common builds.
_RELEASE = r"(?:\d{1,4}!)?\d{1,10}(?:\.\d{1,10}){0,4}"
_SUFFIX = r"(?:(?:a|b|rc)\d{1,4})?(?:\.post\d{1,4})?(?:\.dev\d{1,4})?"
_LOCAL = r"(?:\+(?:\d{1,10}|cpu|cu\d{2,4}|rocm\d{1,3}(?:\.\d{1,3})?|mps|metal|dml))?"
PYTHON_VERSION = re.compile(r"\d{1,2}\.\d{1,3}(?:\.\d{1,3})?(?:(?:a|b|rc)\d{1,3})?\+?")
MODEL_REVISION = re.compile(r"[0-9a-f]{40}")
NOT_INSTALLED: Final = "not installed"
PACKAGE_VERSION = re.compile(rf"{_RELEASE}{_SUFFIX}{_LOCAL}|{NOT_INSTALLED}|unknown")
SECTIONS: Final = ("Question", "Environment", "Method", "Results", "Conclusion")
CONCLUSION_PLACEHOLDER: Final = (
    "To be written after the owner has reviewed these results. Do not accept the ADR from this "
    "draft alone."
)

type Number = int | float | bool


class Kind(Enum):
    """What a metric's value may be. Each spike lists the kind of every metric it can emit, and a
    report read back from a file is refused when a value is outside it (``recall_at_10: -5``,
    ``load_seconds: true``), so a malformed file cannot reach the evidence for an ADR."""

    COUNT = "a whole number, 0 or more"
    RATIO = "a number from 0 to 1"
    AMOUNT = "a number, 0 or more"
    FLAG = "true or false"


# Far above anything a spike measures, and a bound a hand-edited file cannot overshoot silently.
LARGEST_VALUE: Final = 10**12


def in_domain(kind: Kind, value: object) -> bool:
    """Is ``value`` allowed for a metric of this kind? Booleans are not numbers here."""
    if kind is Kind.FLAG:
        return isinstance(value, bool)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    if kind is Kind.COUNT:
        return isinstance(value, int) and 0 <= value <= LARGEST_VALUE
    if kind is Kind.RATIO:
        return 0 <= value <= 1
    return 0 <= value <= LARGEST_VALUE


class SpikeText(NamedTuple):
    """The fixed words of one spike's report: its title, question and method."""

    title: str
    question: str
    method: str


class RowShape(NamedTuple):
    """The metrics one kind of row has: the candidate names it applies to, the metrics every such
    row carries (the context a figure needs to be read: what was measured on, how much was left
    out) and the ones it may carry (a figure a run can lack, such as a peak the system does not
    report). A metric in neither set is one the candidate could never emit. Each set in
    ``together`` is a group of optional figures that a run gives all at once or not at all (the
    scores of one set of questions, the two video-memory figures), so half a group is refused.
    ``consistent`` judges what a row's own numbers say about each other, once every value is known
    to be of its kind: a score exists only when questions were scored, a count of outcomes adds
    up to the count of what was tried. A generated row always satisfies it. ``packages`` are the
    versions this candidate's rows must have, each of a package that was installed (the run needed
    it, so ``not installed`` could not have come from it), in the environment that applies to the
    row: its own, else the report's. Tesseract's is the program's, which is not a Python package."""

    candidate: re.Pattern[str]
    required: frozenset[str]
    optional: frozenset[str] = frozenset()
    together: tuple[frozenset[str], ...] = ()
    consistent: Callable[[Mapping[str, Number]], bool] | None = None
    packages: frozenset[str] = frozenset()

    @property
    def allowed(self) -> frozenset[str]:
        return self.required | self.optional

    def broken_groups(self, names: Collection[str]) -> list[frozenset[str]]:
        """The groups of ``together`` of which ``names`` has some but not all."""
        return [group for group in self.together if 0 < len(group & set(names)) < len(group)]


class SpikeSchema(NamedTuple):
    """Everything a spike's report may contain: its fixed words, the shape of each kind of row it
    can have (a pattern over the candidate names the code can produce, with the metrics such a row
    must and may hold), the kind of value each metric may hold, and the package names its
    environment can list. A report read from a file may use nothing else, because a name that
    merely looks harmless (``patient-diagnosis-confidential``) is still free text, and a row with
    its context missing or with another candidate's figures cannot be read for what it is."""

    text: SpikeText
    rows: tuple[RowShape, ...]
    metrics: Mapping[str, Kind]
    packages: frozenset[str]
    required_packages: frozenset[str] = frozenset()
    # Whether the spike measures a downloaded model, so its environment may name the revision.
    models: bool = False

    def shape_of(self, candidate: str) -> RowShape | None:
        return next((shape for shape in self.rows if shape.candidate.fullmatch(candidate)), None)


def package_version(text: str) -> str:
    """A version as read from a library or a program, or ``unknown`` when it is not in a known
    version shape (a local build label, a word): the report keeps no text it cannot vouch for."""
    return text if PACKAGE_VERSION.fullmatch(text) else "unknown"


def is_machine_text(text: str) -> bool:
    """Is this a machine description: only vocabulary words and model numbers, 100 characters at
    most? Anything else, however harmless it looks, is free text and has no place in a report."""
    if not MACHINE_CHARACTERS.fullmatch(text):
        return False
    words = [word.lower() for word in _MACHINE_SEPARATORS.split(text) if word]
    return bool(words) and all(
        word in MACHINE_WORDS or _MODEL_NUMBER.fullmatch(word) for word in words
    )


def machine_text(text: str) -> str:
    """A description read from the machine, or ``unknown`` when it is not one the report can
    vouch for. It is never cleaned up or cut down: a half-cleaned string is still text."""
    cleaned = text.strip()
    return cleaned if is_machine_text(cleaned) else "unknown"


def system_description() -> str:
    """The operating system as a name and a release number, such as ``Windows 11`` or
    ``Linux 6.18.44``. ``platform.platform()`` carries free-form parts (a distribution's kernel
    label), so only the name and the leading numbers of the release are used."""
    release = re.match(r"\d{1,4}(?:\.\d{1,4}){0,2}", platform.release())
    return machine_text(f"{platform.system()} {release.group(0) if release else ''}")


@dataclass(frozen=True)
class Environment:
    """Facts about the machine and the libraries, so a result can be reproduced."""

    system: str
    python: str
    cpu: str
    cpu_count: int | None
    ram_gib: float | None
    gpu: str | None
    packages: dict[str, str]
    # The Git commit of the model files that were loaded (40 hexadecimal digits), or None when it
    # could not be found. A model repository's default branch moves, so the candidate's name alone
    # does not say which weights, configuration and prompts a row was measured with.
    model_revision: str | None = None

    def __post_init__(self) -> None:
        for label, text in (("system", self.system), ("cpu", self.cpu), ("gpu", self.gpu)):
            if text is not None and not is_machine_text(text):
                msg = f"environment {label} must be a machine description from the known words"
                raise OwnValueError(msg)
        if not PYTHON_VERSION.fullmatch(self.python):
            msg = "environment python must be a version number"
            raise OwnValueError(msg)
        for count in (self.cpu_count, self.ram_gib):
            if count is not None and (
                isinstance(count, bool)
                or not isinstance(count, int | float)
                or not 0 <= count <= 1_000_000
            ):
                msg = "environment cpu_count and ram_gib must be plain numbers or none"
                raise OwnValueError(msg)
        for name, version in self.packages.items():
            if not PACKAGE_NAME.fullmatch(name) or not PACKAGE_VERSION.fullmatch(version):
                msg = "environment packages must be a name and a version number"
                raise OwnValueError(msg)
        if self.model_revision is not None and not (
            isinstance(self.model_revision, str) and MODEL_REVISION.fullmatch(self.model_revision)
        ):
            msg = "environment model revision must be 40 lower-case hexadecimal digits or none"
            raise OwnValueError(msg)

    def to_json(self) -> dict[str, object]:
        return {
            "system": self.system,
            "python": self.python,
            "cpu": self.cpu,
            "cpu_count": self.cpu_count,
            "ram_gib": self.ram_gib,
            "gpu": self.gpu,
            "packages": dict(sorted(self.packages.items())),
            "model_revision": self.model_revision,
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> Environment:
        # Anything that is not the expected type is refused by ``__post_init__``, never converted
        # with ``str()``, so a list or a mapping cannot turn into text on the way in.
        return cls(
            system=cast("str", data["system"]),
            python=cast("str", data["python"]),
            cpu=cast("str", data["cpu"]),
            cpu_count=cast("int | None", data["cpu_count"]),
            ram_gib=cast("float | None", data["ram_gib"]),
            gpu=cast("str | None", data["gpu"]),
            packages=cast("dict[str, str]", data["packages"]),
            model_revision=cast("str | None", data.get("model_revision")),
        )


def _known_environment(data: object, schema: SpikeSchema) -> Environment:
    """An environment read from a file: valid, and listing only packages the spike can list."""
    env = Environment.from_json(cast("dict[str, object]", data))
    if not set(env.packages) <= schema.packages:
        msg = "the report's environment has a package name this spike cannot produce"
        raise OwnValueError(msg)
    if not schema.required_packages <= set(env.packages):
        msg = "the report's environment lacks a package version that every report of this spike has"
        raise OwnValueError(msg)
    if env.model_revision is not None and not schema.models:
        msg = "the report's environment names a model revision, which this spike has none of"
        raise OwnValueError(msg)
    return env


def with_package(env: Environment, name: str, version: str) -> Environment:
    """The environment with one more package version, checked like all the others."""
    return replace(env, packages={**env.packages, name: version})


def with_model_revision(env: Environment, revision: str | None) -> Environment:
    """The environment with the model revision found after the run. Like the GPU name, a value
    that is not 40 hexadecimal digits is stored as none rather than refused: this runs after the
    measurement, and a refusal would throw it away."""
    valid = revision is not None and MODEL_REVISION.fullmatch(revision) is not None
    return replace(env, model_revision=revision if valid else None)


def with_gpu(env: Environment, gpu: str | None) -> Environment:
    """The environment with the GPU found after the run, in the vocabulary of ``machine_text``.

    ``collect_environment`` does the same for a name given up front. A card whose name has a word
    the list lacks (``NVIDIA RTX 4000 Ada Generation``) is stored as ``unknown`` rather than
    refused here, because this runs after the measurement and a refusal would throw it away.
    """
    return replace(env, gpu=None if gpu is None else machine_text(gpu))


def total_ram_gib() -> float | None:
    """Installed memory in GiB, or None if the OS will not say."""
    try:
        if os.name == "nt":  # pragma: no cover - exercised on the Windows runner
            return _total_ram_windows()
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        return None
    return round(float(pages * size) / 1024**3, 1)


def _total_ram_windows() -> float | None:  # pragma: no cover - exercised on the Windows runner
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = (
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        )

    status = Status()
    status.dwLength = ctypes.sizeof(Status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return None
    return round(float(status.ullTotalPhys) / 1024**3, 1)


def collect_environment(packages: tuple[str, ...] = (), gpu: str | None = None) -> Environment:
    """Describe this machine. ``packages`` are distribution names whose versions are recorded."""
    versions: dict[str, str] = {}
    for name in packages:
        try:
            versions[name] = package_version(metadata.version(name))
        except metadata.PackageNotFoundError:
            versions[name] = NOT_INSTALLED
    return Environment(
        system=system_description(),
        python=platform.python_version(),
        cpu=machine_text(platform.processor() or platform.machine()),
        cpu_count=os.cpu_count(),
        ram_gib=total_ram_gib(),
        gpu=None if gpu is None else machine_text(gpu),
        packages=versions,
    )


@dataclass
class Row:
    """One candidate's numbers. ``environment`` is set only when it differs from the report's.

    The embedding workflow runs the CPU candidates, then swaps the CPU-only torch build for a CUDA
    one for the GPU run, so one merged report can honestly hold two environments.
    """

    candidate: str
    metrics: dict[str, Number] = field(default_factory=dict)
    environment: Environment | None = None


def _row_json(row: Row) -> dict[str, object]:
    data: dict[str, object] = {"candidate": row.candidate, "metrics": row.metrics}
    if row.environment is not None:
        data["environment"] = row.environment.to_json()
    return data


def _environment_lines(env: Environment, *, models: bool = False) -> list[str]:
    lines = [
        f"- System: {env.system}",
        f"- Python: {env.python}",
        f"- CPU: {env.cpu} ({env.cpu_count} logical cores)",
        f"- RAM: {env.ram_gib} GiB",
        f"- GPU: {env.gpu or 'none reported'}",
    ]
    if models:
        lines.append(f"- Model revision: {env.model_revision or 'unknown'}")
    return lines + [f"- {name}: {version}" for name, version in sorted(env.packages.items())]


@dataclass
class Report:
    """One spike's results: a fixed question and method, an environment and rows of numbers."""

    spike: str
    title: str
    question: str
    method: str
    environment: Environment
    rows: list[Row] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not SPIKE_NAME.fullmatch(self.spike):
            msg = "spike name must be lower-case letters, digits and hyphens"
            raise OwnValueError(msg)

    def add(self, candidate: str, **metrics: Number) -> None:
        """Add a row. Candidate and metric names are checked, and every value must be a number."""
        if not CANDIDATE.fullmatch(candidate):
            msg = "candidate names are short lower-case labels fixed in code"
            raise OwnValueError(msg)
        row = Row(candidate)
        for name, value in metrics.items():
            if not METRIC.fullmatch(name):
                msg = "metric names are lower_snake_case labels fixed in code"
                raise OwnValueError(msg)
            if not isinstance(value, bool | int | float):
                msg = f"metric {name} must be a number or a boolean, not text"
                raise OwnTypeError(msg)
            if isinstance(value, float) and not math.isfinite(value):
                msg = f"metric {name} must be finite"
                raise OwnValueError(msg)
            row.metrics[name] = round(value, 4) if isinstance(value, float) else value
        self.rows.append(row)

    def to_json(self) -> dict[str, object]:
        return {
            "spike": self.spike,
            "title": self.title,
            "question": self.question,
            "method": self.method,
            "environment": self.environment.to_json(),
            "rows": [_row_json(r) for r in self.rows],
        }

    @classmethod
    def from_json(cls, data: dict[str, object], known: Mapping[str, SpikeSchema]) -> Report:
        """Read a report back, applying every check again (a JSON file is input like any other).

        ``known`` holds the schema of each spike. A report for another spike, or one whose title,
        question or method is anything but the spike's words, or one with a candidate, metric or
        package name the spike cannot produce, is refused: free text in a file would otherwise
        travel into the merged report, which may be written to a tracked folder. The error says
        which kind of name was refused, never what it was.
        """
        spike = data["spike"]
        if not isinstance(spike, str) or spike not in known:
            msg = "the report is not from a known spike"
            raise OwnValueError(msg)
        schema = known[spike]
        fixed = schema.text
        if (data["title"], data["question"], data["method"]) != tuple(fixed):
            msg = "the report's title, question or method is not the spike's own text"
            raise OwnValueError(msg)
        report = cls(
            spike=spike,
            title=fixed.title,
            question=fixed.question,
            method=fixed.method,
            environment=_known_environment(data["environment"], schema),
        )
        for row in cast("list[dict[str, object]]", data["rows"]):
            candidate, metrics = row["candidate"], cast("dict[str, Number]", row["metrics"])
            shape = schema.shape_of(candidate) if isinstance(candidate, str) else None
            if not isinstance(candidate, str) or shape is None:
                msg = "the report has a candidate name this spike cannot produce"
                raise OwnValueError(msg)
            if not isinstance(metrics, dict) or not set(metrics) <= set(schema.metrics):
                msg = "the report has a metric name this spike cannot produce"
                raise OwnValueError(msg)
            if not set(metrics) <= shape.allowed:
                msg = "the report has a metric name this candidate cannot produce"
                raise OwnValueError(msg)
            if not shape.required <= set(metrics):
                msg = "the report has a row without metrics that every row of its candidate has"
                raise OwnValueError(msg)
            if shape.broken_groups(metrics):
                msg = "the report has a row with only part of a group of figures that go together"
                raise OwnValueError(msg)
            report.add(candidate, **metrics)
            for name, value in report.rows[-1].metrics.items():
                if not in_domain(schema.metrics[name], value):
                    msg = f"metric {name} is outside the values this spike can produce"
                    raise OwnValueError(msg)
            if shape.consistent is not None and not shape.consistent(report.rows[-1].metrics):
                msg = "the report has a row whose figures do not fit together"
                raise OwnValueError(msg)
            if (own := row.get("environment")) is not None:
                report.rows[-1].environment = _known_environment(own, schema)
            applies = report.rows[-1].environment or report.environment
            if any(
                applies.packages.get(name, NOT_INSTALLED) == NOT_INSTALLED
                for name in shape.packages
            ):
                msg = "the report has a row whose environment lacks a version its candidate needs"
                raise OwnValueError(msg)
        return report

    def to_markdown(self) -> str:
        lines = [
            f"# Spike: {self.title}",
            "",
            "Draft written by `scripts/spikes`. Metrics only, no document text.",
            "",
            "## Question",
            "",
            self.question,
            "",
            "## Environment",
            "",
        ]
        own: list[tuple[str, Environment]] = []
        shared: list[str] = []
        for row in self.rows:
            if row.environment is not None and row.environment != self.environment:
                own.append((row.candidate, row.environment))
            else:
                shared.append(row.candidate)
        if own:
            lines += [f"Environment of {', '.join(shared) or 'the other candidates'}:", ""]
        models = self.spike == "embedding"  # the only spike that loads a downloaded model
        lines += _environment_lines(self.environment, models=models)
        for candidate, env in own:
            lines += ["", f"Environment of {candidate}:", ""]
            lines += _environment_lines(env, models=models)
        lines += ["", "## Method", "", self.method, "", "## Results", ""]
        metric_names = sorted({name for row in self.rows for name in row.metrics})
        lines.append("| Candidate | " + " | ".join(metric_names) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in metric_names) + " |")
        for row in self.rows:
            cells = [str(row.metrics.get(name, "")) for name in metric_names]
            lines.append(f"| {row.candidate} | " + " | ".join(cells) + " |")
        lines += ["", "## Conclusion", "", CONCLUSION_PLACEHOLDER, ""]
        return "\n".join(lines)

    def write(
        self, directory: Path, stem: str | None = None, *, overwrite: bool = False
    ) -> tuple[Path, Path]:
        """Write ``<stem>.json`` and ``<stem>.md`` into ``directory`` and return both paths.

        A measurement can cost an hour on the owner's PC, so an existing file is never
        overwritten silently: ``overwrite`` is for a file that is only derived from others (a
        merged report). The CLI calls :func:`require_free` before a run starts, so a name clash
        is found before the hour is spent, not after."""
        stem = self.spike if stem is None else stem
        if not overwrite:
            require_free(directory, stem)
        elif not _STEM.fullmatch(stem):
            raise OwnValueError(_STEM_MESSAGE)
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / f"{stem}.json"
        md_path = directory / f"{stem}.md"
        json_path.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, md_path


_STEM = re.compile(r"[a-z0-9][a-z0-9._-]{0,80}")
_STEM_MESSAGE = "the file name stem must be lower-case letters, digits, dots and hyphens"


def require_free(directory: Path, stem: str) -> None:
    """Refuse a bad stem, a report of that name that is already in ``directory`` (a link counts,
    even a dangling one, as ``Path.exists`` follows links and would miss it), or a ``directory``
    that cannot be made and written to. It makes the folder if it is missing."""
    if not _STEM.fullmatch(stem):
        raise OwnValueError(_STEM_MESSAGE)
    if any(os.path.lexists(directory / f"{stem}{suffix}") for suffix in (".json", ".md")):
        msg = (
            "a report with this name is already in the output folder; "
            "move it, or use another --out folder"
        )
        raise OwnValueError(msg)
    # The folder is made and tried now, not when the report is written: a protected or misspelled
    # --out found after an hour of measuring would throw the measurement away.
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=directory):
            pass
    except OSError:
        msg = "the output folder cannot be created or written to; use another --out folder"
        raise OwnValueError(msg) from None


def merge(reports: list[Report]) -> Report:
    """Combine reports of the same spike (one candidate per process) into one."""
    if not reports:
        msg = "nothing to merge"
        raise OwnValueError(msg)
    first = reports[0]
    for other in reports[1:]:
        if (other.spike, other.question, other.method) != (
            first.spike,
            first.question,
            first.method,
        ):
            msg = "reports are not from the same spike"
            raise OwnValueError(msg)
    merged = Report(first.spike, first.title, first.question, first.method, first.environment)
    for report in reports:
        for row in report.rows:
            own = row.environment or (
                report.environment if report.environment != first.environment else None
            )
            merged.rows.append(Row(row.candidate, dict(row.metrics), own))
    return merged
