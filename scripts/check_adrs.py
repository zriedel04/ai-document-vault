#!/usr/bin/env python3
"""Check the structure of the architecture decision records in docs/decisions/.

Each ADR must have a status, a date, a hardware-dependence flag and a plan
reference, plus Context, Decision, Alternatives considered and Consequences
sections. A hardware-dependent ADR may only be Accepted once it links a spike result file
with its sections filled in, a Proposed ADR needs an "Evidence needed" section, and the index in
docs/decisions/README.md must list every ADR with its current status. "Superseded by ADR N" and
"Supersedes: ADR N" must agree, and the replacement must be an Accepted, newer ADR.

Standard library only. Usage:
    python scripts/check_adrs.py [ROOT]    # ROOT defaults to the repository root
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

ADR_FILE = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
SUPERSEDED = re.compile(r"^Superseded by ADR (\d{4})$")
SPIKE_FILE = re.compile(r"^docs/spikes/\d{2}-[a-z0-9-]+\.md$")
SPIKE_SECTIONS = ("Question", "Environment", "Method", "Results", "Conclusion")
SUPERSEDES = re.compile(r"^ADR (\d{4})(?:, ADR (\d{4}))*$")
ADR_NUMBER = re.compile(r"ADR (\d{4})")
NOT_ADRS = ("README.md", "0000-template.md")
STATUSES = ("Proposed", "Accepted", "Rejected")
REQUIRED_SECTIONS = ("Context", "Decision", "Alternatives considered", "Consequences")
INDEX_ROW = re.compile(r"^\|\s*\[(\d{4})\]\(([^)]+)\)\s*\|[^|]*\|\s*([^|]+?)\s*\|\s*$")


def _field(text: str, name: str) -> str | None:
    match = re.search(rf"^- {re.escape(name)}:[ \t]*(.+?)[ \t]*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^## +(.+?) *$", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def _spike_problem(spike: str | None, root: Path) -> bool:
    """True unless ``spike`` is an existing docs/spikes/NN-topic.md with every section filled.

    This is a structural check: it cannot prove the experiment was run on the owner's Windows PC.
    That judgement stays with the owner's review of the spike results and of the ADR change.
    """
    if spike is None or not SPIKE_FILE.match(spike):
        return True
    path = root / spike
    folder = (root / "docs" / "spikes").resolve()
    if not path.is_file() or not path.resolve().is_relative_to(folder):
        return True
    sections = _sections(path.read_text(encoding="utf-8"))
    return not all(sections.get(name) for name in SPIKE_SECTIONS)


class Adr(NamedTuple):
    problems: list[str]
    status: str | None
    supersedes: frozenset[int]


def check_adr(path: Path, root: Path) -> Adr:
    """Problems with one ADR, its status and the ADRs it says it supersedes."""
    text = path.read_text(encoding="utf-8")
    name = path.name
    problems: list[str] = []
    number = name[:4]

    if not re.match(rf"^# ADR {number}: \S", text):
        problems.append(f"{name}: first line must be '# ADR {number}: <title>'")

    status = _field(text, "Status")
    if status is None:
        problems.append(f"{name}: missing '- Status:' line")
    elif status not in STATUSES and not SUPERSEDED.match(status):
        problems.append(
            f"{name}: status '{status}' must be Proposed, Accepted, Rejected or "
            "'Superseded by ADR NNNN'"
        )

    date = _field(text, "Date")
    if date is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        problems.append(f"{name}: '- Date:' must be YYYY-MM-DD")

    hardware = _field(text, "Hardware-dependent")
    if hardware not in ("yes", "no"):
        problems.append(f"{name}: '- Hardware-dependent:' must be yes or no")

    if not _field(text, "Plan reference"):
        problems.append(f"{name}: missing '- Plan reference:' line")

    supersedes_field = _field(text, "Supersedes")
    supersedes: frozenset[int] = frozenset()
    if supersedes_field is not None:
        if SUPERSEDES.match(supersedes_field):
            supersedes = frozenset(int(n) for n in ADR_NUMBER.findall(supersedes_field))
        else:
            problems.append(
                f"{name}: '- Supersedes:' must look like 'ADR 0001' or 'ADR 0001, ADR 0002'"
            )

    sections = _sections(text)
    for section in REQUIRED_SECTIONS:
        if not sections.get(section):
            problems.append(f"{name}: missing or empty '## {section}' section")

    if status == "Proposed" and not sections.get("Evidence needed"):
        problems.append(f"{name}: a Proposed ADR needs an '## Evidence needed' section")

    if hardware == "yes" and status == "Accepted":
        if _spike_problem(_field(text, "Spike results"), root):
            problems.append(
                f"{name}: hardware-dependent ADRs cannot be Accepted without "
                "'- Spike results: docs/spikes/NN-topic.md' pointing at an existing spike "
                f"result file with non-empty {', '.join(SPIKE_SECTIONS)} sections"
            )
    return Adr(problems, status, supersedes)


def supersession_problems(adrs: dict[int, Adr]) -> list[str]:
    """Both directions of a supersession must agree, and the replacement must be in force.

    ADR A is "Superseded by ADR B" only if B exists, is newer, is itself Accepted (or has since
    been superseded in turn) and says "Supersedes: ADR A". A "Supersedes" line must point at an
    older ADR whose status names the same replacement.
    """
    problems: list[str] = []
    for number, adr in sorted(adrs.items()):
        label = f"{number:04d}"
        match = SUPERSEDED.match(adr.status) if adr.status else None
        if match:
            replacement = int(match.group(1))
            new = adrs.get(replacement)
            if new is None or replacement <= number:
                problems.append(
                    f"{label}: '{adr.status}' must name an existing, newer ADR "
                    f"(existing: {sorted(adrs)})"
                )
            elif not (new.status == "Accepted" or (new.status or "").startswith("Superseded by")):
                problems.append(
                    f"{label}: replacement ADR {replacement:04d} is '{new.status}', "
                    "it must be Accepted"
                )
            elif number not in new.supersedes:
                problems.append(
                    f"{label}: ADR {replacement:04d} must say '- Supersedes: ADR {label}'"
                )
        for old in sorted(adr.supersedes):
            target = adrs.get(old)
            if old >= number or target is None:
                problems.append(f"{label}: 'Supersedes: ADR {old:04d}' must name an older ADR")
            elif target.status != f"Superseded by ADR {label}":
                problems.append(
                    f"{label}: supersedes ADR {old:04d}, whose status must be "
                    f"'Superseded by ADR {label}', not '{target.status}'"
                )
    return problems


def adr_files(root: Path) -> list[Path]:
    folder = root / "docs" / "decisions"
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and ADR_FILE.match(path.name) and path.name not in NOT_ADRS
    )


def stray_files(root: Path) -> list[Path]:
    """Markdown files in docs/decisions/ that are neither an ADR nor README/template."""
    folder = root / "docs" / "decisions"
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".md"
        and path.name not in NOT_ADRS
        and not ADR_FILE.match(path.name)
    )


def check(root: Path) -> list[str]:
    folder = root / "docs" / "decisions"
    problems: list[str] = []
    adrs = adr_files(root)
    problems.extend(
        f"{path.name}: not a valid ADR file name (use NNNN-lowercase-title.md)"
        for path in stray_files(root)
    )
    if not adrs:
        return [*problems, f"no ADRs found in {folder}"]

    numbers = [int(path.name[:4]) for path in adrs]
    if numbers != list(range(1, len(adrs) + 1)):
        problems.append(f"ADR numbers must run 0001..{len(adrs):04d} without gaps: {numbers}")

    statuses: dict[str, str | None] = {}
    parsed: dict[int, Adr] = {}
    for path in adrs:
        adr = check_adr(path, root)
        problems.extend(adr.problems)
        statuses[path.name] = adr.status
        parsed[int(path.name[:4])] = adr
    problems.extend(supersession_problems(parsed))

    index = folder / "README.md"
    if not index.is_file():
        return [*problems, "docs/decisions/README.md (the index) is missing"]
    listed: dict[str, str] = {}
    for line in index.read_text(encoding="utf-8").splitlines():
        match = INDEX_ROW.match(line)
        if match:
            listed[match.group(2)] = match.group(3)
    for name, status in statuses.items():
        if name not in listed:
            problems.append(f"index: {name} is not listed in docs/decisions/README.md")
        elif status is not None and listed[name] != status:
            problems.append(
                f"index: {name} is '{listed[name]}' in the index but '{status}' in the file"
            )
    for name in listed:
        if name not in statuses:
            problems.append(f"index: lists {name}, which does not exist")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]) if args else Path(__file__).resolve().parent.parent
    problems = check(root)
    if problems:
        print("ADR check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"ADR check passed: {len(adr_files(root))} ADRs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
