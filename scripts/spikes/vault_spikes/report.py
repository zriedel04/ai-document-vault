"""Spike reports: metrics only, never document text.

A report is built from names fixed in code (a spike, a candidate, a metric) and numbers. Its
title, question and method are fixed text per spike, and the only other strings (the machine and
package descriptions) are checked against short, strict patterns, so a script cannot put document
text, a file name or an OCR result into a report by accident: anything else is a TypeError or
ValueError. A report read back from a JSON file is checked the same way, and its title, question
and method must equal the fixed text of the spike it names. The Markdown output has the five
sections that ``scripts/check_adrs.py`` requires of a spike file, with the conclusion left for the
owner's review.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from importlib import metadata
from pathlib import Path
from typing import Final, NamedTuple, cast

# Always use ``fullmatch`` with these patterns (``$`` also matches before a trailing newline).
SPIKE_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
CANDIDATE = re.compile(r"[a-z0-9][a-z0-9._+-]{0,60}")
METRIC = re.compile(r"[a-z][a-z0-9_]{0,48}")
# A machine or GPU description such as "AMD64 Family 25 Model 97, AuthenticAMD": letters, digits and
# a few marks, at most 100 characters. This is the one free-form text left in a report, and a
# pattern cannot tell a CPU name from a short sentence, so the owner reviews the environment lines
# of a report before committing it (docs/spikes/README.md).
MACHINE_TEXT = re.compile(r"[A-Za-z0-9 ._+()/,:?-]{1,100}")
PYTHON_VERSION = re.compile(r"\d{1,2}\.\d{1,3}(?:\.\d{1,3})?[A-Za-z0-9.+-]{0,20}")
PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}")
PACKAGE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,40}|not installed")
SECTIONS: Final = ("Question", "Environment", "Method", "Results", "Conclusion")
CONCLUSION_PLACEHOLDER: Final = (
    "To be written after the owner has reviewed these results. Do not accept the ADR from this "
    "draft alone."
)

type Number = int | float | bool


class SpikeText(NamedTuple):
    """The fixed words of one spike's report: its title, question and method."""

    title: str
    question: str
    method: str


def machine_text(text: str) -> str:
    """Make a description from the OS fit the report: unusual characters become ``?``."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._+()/,:?-]", "?", text).strip()[:100]
    return cleaned or "unknown"


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

    def __post_init__(self) -> None:
        for label, text in (("system", self.system), ("cpu", self.cpu), ("gpu", self.gpu)):
            if text is not None and not MACHINE_TEXT.fullmatch(text):
                msg = f"environment {label} must be a short machine description"
                raise ValueError(msg)
        if not PYTHON_VERSION.fullmatch(self.python):
            msg = "environment python must be a version number"
            raise ValueError(msg)
        for count in (self.cpu_count, self.ram_gib):
            if count is not None and (
                isinstance(count, bool)
                or not isinstance(count, int | float)
                or not 0 <= count <= 1_000_000
            ):
                msg = "environment cpu_count and ram_gib must be plain numbers or none"
                raise ValueError(msg)
        for name, version in self.packages.items():
            if not PACKAGE_NAME.fullmatch(name) or not PACKAGE_VERSION.fullmatch(version):
                msg = "environment packages must be a name and a version number"
                raise ValueError(msg)

    def to_json(self) -> dict[str, object]:
        return {
            "system": self.system,
            "python": self.python,
            "cpu": self.cpu,
            "cpu_count": self.cpu_count,
            "ram_gib": self.ram_gib,
            "gpu": self.gpu,
            "packages": dict(sorted(self.packages.items())),
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
        )


def with_package(env: Environment, name: str, version: str) -> Environment:
    """The environment with one more package version, checked like all the others."""
    return replace(env, packages={**env.packages, name: version})


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
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return Environment(
        system=machine_text(platform.platform()),
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


def _environment_lines(env: Environment) -> list[str]:
    lines = [
        f"- System: {env.system}",
        f"- Python: {env.python}",
        f"- CPU: {env.cpu} ({env.cpu_count} logical cores)",
        f"- RAM: {env.ram_gib} GiB",
        f"- GPU: {env.gpu or 'none reported'}",
    ]
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
            raise ValueError(msg)

    def add(self, candidate: str, **metrics: Number) -> None:
        """Add a row. Candidate and metric names are checked, and every value must be a number."""
        if not CANDIDATE.fullmatch(candidate):
            msg = "candidate names are short lower-case labels fixed in code"
            raise ValueError(msg)
        row = Row(candidate)
        for name, value in metrics.items():
            if not METRIC.fullmatch(name):
                msg = "metric names are lower_snake_case labels fixed in code"
                raise ValueError(msg)
            if not isinstance(value, bool | int | float):
                msg = f"metric {name} must be a number or a boolean, not text"
                raise TypeError(msg)
            if isinstance(value, float) and not math.isfinite(value):
                msg = f"metric {name} must be finite"
                raise ValueError(msg)
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
    def from_json(cls, data: dict[str, object], known: Mapping[str, SpikeText]) -> Report:
        """Read a report back, applying every check again (a JSON file is input like any other).

        ``known`` holds the fixed words of each spike. A report for another spike, or one whose
        title, question or method is anything but those words, is refused: free text in a file
        would otherwise travel into the merged report, which may be written to a tracked folder.
        """
        spike = data["spike"]
        if not isinstance(spike, str) or spike not in known:
            msg = "the report is not from a known spike"
            raise ValueError(msg)
        fixed = known[spike]
        if (data["title"], data["question"], data["method"]) != tuple(fixed):
            msg = "the report's title, question or method is not the spike's own text"
            raise ValueError(msg)
        report = cls(
            spike=spike,
            title=fixed.title,
            question=fixed.question,
            method=fixed.method,
            environment=Environment.from_json(cast("dict[str, object]", data["environment"])),
        )
        for row in cast("list[dict[str, object]]", data["rows"]):
            report.add(str(row["candidate"]), **cast("dict[str, Number]", row["metrics"]))
            if (own := row.get("environment")) is not None:
                report.rows[-1].environment = Environment.from_json(cast("dict[str, object]", own))
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
        lines += _environment_lines(self.environment)
        for candidate, env in own:
            lines += ["", f"Environment of {candidate}:", ""]
            lines += _environment_lines(env)
        lines += ["", "## Method", "", self.method, "", "## Results", ""]
        metric_names = sorted({name for row in self.rows for name in row.metrics})
        lines.append("| Candidate | " + " | ".join(metric_names) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in metric_names) + " |")
        for row in self.rows:
            cells = [str(row.metrics.get(name, "")) for name in metric_names]
            lines.append(f"| {row.candidate} | " + " | ".join(cells) + " |")
        lines += ["", "## Conclusion", "", CONCLUSION_PLACEHOLDER, ""]
        return "\n".join(lines)

    def write(self, directory: Path, stem: str | None = None) -> tuple[Path, Path]:
        """Write ``<stem>.json`` and ``<stem>.md`` into ``directory`` and return both paths."""
        stem = self.spike if stem is None else stem
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,80}", stem):
            msg = "the file name stem must be lower-case letters, digits, dots and hyphens"
            raise ValueError(msg)
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / f"{stem}.json"
        md_path = directory / f"{stem}.md"
        json_path.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, md_path


def merge(reports: list[Report]) -> Report:
    """Combine reports of the same spike (one candidate per process) into one."""
    if not reports:
        msg = "nothing to merge"
        raise ValueError(msg)
    first = reports[0]
    for other in reports[1:]:
        if (other.spike, other.question, other.method) != (
            first.spike,
            first.question,
            first.method,
        ):
            msg = "reports are not from the same spike"
            raise ValueError(msg)
    merged = Report(first.spike, first.title, first.question, first.method, first.environment)
    for report in reports:
        for row in report.rows:
            own = row.environment or (
                report.environment if report.environment != first.environment else None
            )
            merged.rows.append(Row(row.candidate, dict(row.metrics), own))
    return merged
