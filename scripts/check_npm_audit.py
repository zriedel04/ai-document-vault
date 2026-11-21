#!/usr/bin/env python3
"""Fail if `npm audit` reports an advisory for frontend/ that is not a reviewed, current exemption.

CI used to run `npm audit` on its own, which fails on any advisory of any severity. That stays the
rule: every advisory fails the build, except one listed in EXEMPTIONS below, and an exemption holds
only while all of these are true:

- the advisory is for the package the exemption names;
- the exemption has not expired (UTC date; an expired exemption fails and must be reviewed again);
- every copy of that package in frontend/package-lock.json is development-only, so it never reaches
  the built app. A copy is found by the package it installs (the entry's `name`, which npm writes
  for an alias such as `"glob": "npm:picomatch@4"`), not by its folder, and every path npm reports
  for the package must be one of those copies;
- no fixed release exists: the newest published version of the package is still inside the
  advisory's vulnerable range. Once a fix is published the exemption stops applying and the build
  fails until the lock file is updated, so an exemption cannot outlive the reason for it.

A package npm marks vulnerable only because it depends on an excused package is excused with it;
one that depends on anything else is not. Anything the script cannot read (an audit report of
another shape, an advisory link that is not a GitHub advisory id, a version range it does not
understand, a lookup that fails) fails the build: nothing is assumed to be fine.

Standard library only. Usage (run from the repository root; it calls npm in frontend/):
    python scripts/check_npm_audit.py
Tests pass the audit report, lock file, latest versions and date in directly.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@dataclass(frozen=True)
class Exemption:
    """A reviewed advisory: the package it is for, until when it holds, and why."""

    package: str
    expires: date
    reason: str


# Reviewed advisories (CONTRIBUTING.md, "Dependency audit"). Key: the GitHub advisory id.
EXEMPTIONS: dict[str, Exemption] = {
    "GHSA-fc5q-39xc-vjpf": Exemption(
        package="picomatch",
        expires=date(2026, 12, 31),
        reason=(
            "ReDoS through repeated extglobs in glob patterns. Every release up to 4.0.7 is "
            "affected and none is fixed (2026-11-17). Only Vite, Vitest and typescript-eslint use "
            "it, on the project's own glob patterns at build and test time; it is not in the built "
            "app. Approved by the owner on 2026-11-17."
        ),
    ),
}

ADVISORY_URL = re.compile(
    r"https://github\.com/advisories/(GHSA(?:-[23456789cfghjmpqrvwx]{4}){3})", re.ASCII
)
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)
COMPARATOR = re.compile(r"(<=|>=|<|>|=)?(.+)", re.ASCII)


class AuditError(Exception):
    """The audit report, lock file or registry answer could not be read as expected."""


def parse_version(text: str) -> tuple[int, int, int]:
    """A plain release version; a prerelease or anything else is refused."""
    match = VERSION.fullmatch(text)
    if match is None:
        raise AuditError(f"not a release version: {text!r}")
    return int(match[1]), int(match[2]), int(match[3])


def in_range(version: str, spec: str) -> bool:
    """Whether a release version is inside an npm range made of comparators, `||` and `*`."""
    target = parse_version(version)
    alternatives = spec.split("||")
    for alternative in alternatives:
        parts = alternative.split()
        if not parts:
            raise AuditError(f"empty range in {spec!r}")
        if parts == ["*"]:
            return True
        if all(_holds(target, part) for part in parts):
            return True
    return False


def _holds(target: tuple[int, int, int], comparator: str) -> bool:
    match = COMPARATOR.fullmatch(comparator)
    if match is None:
        raise AuditError(f"unreadable comparator {comparator!r}")
    bound = parse_version(match[2])
    operator = match[1] or "="
    return {
        "<": target < bound,
        "<=": target <= bound,
        ">": target > bound,
        ">=": target >= bound,
        "=": target == bound,
    }[operator]


@dataclass(frozen=True)
class Advisory:
    advisory_id: str
    package: str
    severity: str
    vulnerable: str


def read_report(report: object) -> dict[str, list[object]]:
    """The `via` list of each vulnerable package in an `npm audit --json` report (version 2)."""
    if not isinstance(report, dict) or report.get("auditReportVersion") != 2:
        raise AuditError("not an npm audit report of version 2")
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise AuditError("the report has no vulnerabilities table")
    result: dict[str, list[object]] = {}
    for name, entry in vulnerabilities.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("via"), list) or not entry["via"]:
            raise AuditError(f"entry {name!r} has no via list")
        result[name] = entry["via"]
    return result


def read_nodes(report: Mapping[str, object]) -> dict[str, list[str]]:
    """The installed paths (`nodes`) npm reports for each vulnerable package."""
    vulnerabilities = report["vulnerabilities"]
    assert isinstance(vulnerabilities, dict)  # checked by read_report
    result: dict[str, list[str]] = {}
    for name, entry in vulnerabilities.items():
        nodes = entry.get("nodes")
        if not isinstance(nodes, list) or not nodes or not all(isinstance(n, str) for n in nodes):
            raise AuditError(f"entry {name!r} has no list of installed paths")
        result[name] = nodes
    return result


def read_advisory(item: Mapping[str, object]) -> Advisory:
    url, name, severity, vulnerable = (item.get(k) for k in ("url", "name", "severity", "range"))
    if not all(isinstance(v, str) for v in (url, name, severity, vulnerable)):
        raise AuditError("an advisory lacks its url, name, severity or range")
    assert isinstance(url, str) and isinstance(name, str)  # for the type checker
    match = ADVISORY_URL.fullmatch(url)
    if match is None:
        raise AuditError(f"advisory link is not a GitHub advisory: {url!r}")
    return Advisory(match[1], name, str(severity), str(vulnerable))


def installed_name(path: str, info: object) -> str | None:
    """The real name of the package a lock entry installs: its `name` field, else its folder.

    npm writes `name` when it differs from the folder, as for an alias (`"glob": "npm:picomatch@4"`
    installs picomatch at `node_modules/glob`), so a copy is found by what it is, not where it sits.
    """
    if isinstance(info, dict) and "name" in info:
        name = info["name"]
        if not isinstance(name, str) or not name:
            raise AuditError(f"lock entry {path!r} has an unreadable name")
        return name
    marker = "node_modules/"
    if marker not in path:
        return None
    return path.rsplit(marker, 1)[1]


def lock_copies(lock: object, package: str) -> dict[str, bool]:
    """Each copy of `package` in a package-lock.json (version 2 or 3): its path and whether it is
    development-only. The root entry (`""`) is the project itself and is not a copy."""
    if not isinstance(lock, dict) or not isinstance(lock.get("packages"), dict):
        raise AuditError("not a package-lock.json with a packages table")
    copies: dict[str, bool] = {}
    for path, info in lock["packages"].items():
        if not isinstance(path, str):
            raise AuditError("the lock file has a path that is not text")
        if path == "":
            continue
        if installed_name(path, info) == package:
            copies[path] = isinstance(info, dict) and info.get("dev") is True
    return copies


def excuse(
    advisory: Advisory,
    *,
    nodes: Sequence[str],
    lock: object,
    latest: Callable[[str], str],
    today: date,
) -> str | None:
    """None if the advisory is a current exemption, otherwise why it fails the build."""
    exemption = EXEMPTIONS.get(advisory.advisory_id)
    if exemption is None:
        return "not a reviewed exemption"
    if exemption.package != advisory.package:
        return f"the exemption is for {exemption.package}, not {advisory.package}"
    if today > exemption.expires:
        return f"the exemption expired on {exemption.expires.isoformat()}; review it again"
    copies = lock_copies(lock, advisory.package)
    if not copies:
        return "the package is not in the lock file"
    if not all(copies.values()):
        return "a copy of the package is not development-only"
    if not set(nodes) <= set(copies):
        return "npm reports a copy the lock file does not list as this package"
    newest = latest(advisory.package)
    if not in_range(newest, advisory.vulnerable):
        return f"version {newest} is outside the vulnerable range {advisory.vulnerable}: update"
    return None


def check(
    report: object,
    lock: object,
    *,
    latest: Callable[[str], str],
    today: date,
) -> tuple[list[str], list[str]]:
    """Failures, and notices for the advisories excused. Raises AuditError on unreadable input."""
    vias = read_report(report)
    assert isinstance(report, dict)
    nodes = read_nodes(report)
    advisories: dict[str, Advisory] = {}
    direct: dict[str, list[str]] = {name: [] for name in vias}
    depends: dict[str, list[str]] = {name: [] for name in vias}
    for name, via in vias.items():
        for item in via:
            if isinstance(item, str):
                if item not in vias:
                    raise AuditError(f"{name!r} depends on {item!r}, which the report does not list")
                depends[name].append(item)
            elif isinstance(item, dict):
                advisory = read_advisory(item)
                advisories[advisory.advisory_id] = advisory
                direct[name].append(advisory.advisory_id)
            else:
                raise AuditError(f"entry {name!r} has an unreadable via item")

    failures: list[str] = []
    notices: list[str] = []
    excused: set[str] = set()
    for advisory_id in sorted(advisories):
        advisory = advisories[advisory_id]
        if advisory.package not in nodes:
            raise AuditError(f"no report entry for {advisory.package!r}, which {advisory_id} names")
        why = excuse(advisory, nodes=nodes[advisory.package], lock=lock, latest=latest, today=today)
        label = f"{advisory_id} ({advisory.severity}) in {advisory.package} {advisory.vulnerable}"
        if why is None:
            excused.add(advisory_id)
            exemption = EXEMPTIONS[advisory_id]
            notices.append(f"{label}: excused until {exemption.expires.isoformat()}")
        else:
            failures.append(f"{label}: {why}")

    # A package is excused when everything that makes it vulnerable is excused (a fixed point, so
    # a dependency cycle is excused only if something outside it is).
    clean: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name in vias:
            if name in clean:
                continue
            if all(a in excused for a in direct[name]) and all(d in clean for d in depends[name]):
                clean.add(name)
                changed = True
    for name in sorted(set(vias) - clean):
        if not direct[name]:
            failures.append(f"{name}: vulnerable through packages that are not excused")
    return failures, notices


def _npm(*args: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("npm")
    if executable is None:
        raise AuditError("npm is not installed")
    return subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [executable, *args], cwd=FRONTEND, capture_output=True, text=True, check=False
    )


def npm_latest(package: str) -> str:
    result = _npm("view", package, "version")
    if result.returncode != 0:
        raise AuditError(f"npm view {package} failed")
    return result.stdout.strip()


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        print("usage: python scripts/check_npm_audit.py", file=sys.stderr)
        return 2
    try:
        result = _npm("audit", "--json")
        report = json.loads(result.stdout)
        lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
        failures, notices = check(
            report, lock, latest=npm_latest, today=datetime.now(UTC).date()
        )
    except (AuditError, OSError, ValueError) as error:
        print(f"npm audit check could not run: {error}", file=sys.stderr)
        return 1
    for notice in notices:
        print(f"excused: {notice}")
    for failure in failures:
        print(f"advisory: {failure}", file=sys.stderr)
    if failures:
        return 1
    print("npm audit: no advisory outside the reviewed exemptions")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
