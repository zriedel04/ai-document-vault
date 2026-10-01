#!/usr/bin/env python3
"""Check the structure of the architecture decision records in docs/decisions/.

Each ADR must have a status, a date, a hardware-dependence flag and a plan
reference, plus Context, Decision, Alternatives considered and Consequences
sections. A hardware-dependent ADR may only be Accepted once it links a spike
result, a Proposed ADR needs an "Evidence needed" section, and the index in
docs/decisions/README.md must list every ADR with its current status.

Standard library only. Usage:
    python scripts/check_adrs.py [ROOT]    # ROOT defaults to the repository root
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

ADR_FILE = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
SUPERSEDED = re.compile(r"^Superseded by ADR \d{4}$")
STATUSES = ("Proposed", "Accepted", "Rejected")
REQUIRED_SECTIONS = ("Context", "Decision", "Alternatives considered", "Consequences")
INDEX_ROW = re.compile(r"^\|\s*\[(\d{4})\]\(([^)]+)\)\s*\|[^|]*\|\s*([^|]+?)\s*\|\s*$")


def _field(text: str, name: str) -> str | None:
    match = re.search(rf"^- {re.escape(name)}:[ \t]*(.+?)[ \t]*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^## +(.+?) *$", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def check_adr(path: Path, root: Path) -> tuple[list[str], str | None]:
    """Problems with one ADR, and its status."""
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

    sections = _sections(text)
    for section in REQUIRED_SECTIONS:
        if not sections.get(section):
            problems.append(f"{name}: missing or empty '## {section}' section")

    if status == "Proposed" and not sections.get("Evidence needed"):
        problems.append(f"{name}: a Proposed ADR needs an '## Evidence needed' section")

    if hardware == "yes" and status == "Accepted":
        spike = _field(text, "Spike results")
        if spike is None or not (root / spike).is_file():
            problems.append(
                f"{name}: hardware-dependent ADRs cannot be Accepted without "
                "'- Spike results: docs/spikes/<file>.md' pointing at an existing file"
            )
    return problems, status


def adr_files(root: Path) -> list[Path]:
    folder = root / "docs" / "decisions"
    return sorted(
        path
        for path in folder.glob("*.md")
        if ADR_FILE.match(path.name) and path.name != "0000-template.md"
    )


def check(root: Path) -> list[str]:
    folder = root / "docs" / "decisions"
    problems: list[str] = []
    adrs = adr_files(root)
    if not adrs:
        return [f"no ADRs found in {folder}"]

    numbers = [int(path.name[:4]) for path in adrs]
    if numbers != list(range(1, len(adrs) + 1)):
        problems.append(f"ADR numbers must run 0001..{len(adrs):04d} without gaps: {numbers}")

    statuses: dict[str, str | None] = {}
    for path in adrs:
        adr_problems, status = check_adr(path, root)
        problems.extend(adr_problems)
        statuses[path.name] = status

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
