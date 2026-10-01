"""Spike reports: metrics only, never document text.

A report is built from names fixed in code (a spike, a candidate, a metric) and numbers. There is
no free-text field, so a script cannot put document text, a file name or an OCR result into a
report by accident: the only strings are checked against strict patterns, and anything else is a
TypeError or ValueError. The Markdown output has the five sections that ``scripts/check_adrs.py``
requires of a spike file, with the conclusion left for the owner's review.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Final, cast

# Always use ``fullmatch`` with these patterns (``$`` also matches before a trailing newline).
SPIKE_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
CANDIDATE = re.compile(r"[a-z0-9][a-z0-9._+-]{0,60}")
METRIC = re.compile(r"[a-z][a-z0-9_]{0,48}")
SECTIONS: Final = ("Question", "Environment", "Method", "Results", "Conclusion")
CONCLUSION_PLACEHOLDER: Final = (
    "To be written after the owner has reviewed these results. Do not accept the ADR from this "
    "draft alone."
)

type Number = int | float | bool


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
        packages = cast("dict[str, str]", data["packages"])
        return cls(
            system=str(data["system"]),
            python=str(data["python"]),
            cpu=str(data["cpu"]),
            cpu_count=cast("int | None", data["cpu_count"]),
            ram_gib=cast("float | None", data["ram_gib"]),
            gpu=cast("str | None", data["gpu"]),
            packages=dict(packages),
        )


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
        system=platform.platform(),
        python=platform.python_version(),
        cpu=platform.processor() or platform.machine() or "unknown",
        cpu_count=os.cpu_count(),
        ram_gib=total_ram_gib(),
        gpu=gpu,
        packages=versions,
    )


@dataclass
class Row:
    candidate: str
    metrics: dict[str, Number] = field(default_factory=dict)


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
            "rows": [{"candidate": r.candidate, "metrics": r.metrics} for r in self.rows],
        }

    @classmethod
    def from_json(cls, data: dict[str, object]) -> Report:
        """Read a report back, applying every check again (a JSON file is input like any other)."""
        report = cls(
            spike=str(data["spike"]),
            title=str(data["title"]),
            question=str(data["question"]),
            method=str(data["method"]),
            environment=Environment.from_json(cast("dict[str, object]", data["environment"])),
        )
        for row in cast("list[dict[str, object]]", data["rows"]):
            report.add(str(row["candidate"]), **cast("dict[str, Number]", row["metrics"]))
        return report

    def to_markdown(self) -> str:
        env = self.environment
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
            f"- System: {env.system}",
            f"- Python: {env.python}",
            f"- CPU: {env.cpu} ({env.cpu_count} logical cores)",
            f"- RAM: {env.ram_gib} GiB",
            f"- GPU: {env.gpu or 'none reported'}",
        ]
        lines += [f"- {name}: {version}" for name, version in sorted(env.packages.items())]
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
        merged.rows.extend(report.rows)
    return merged
