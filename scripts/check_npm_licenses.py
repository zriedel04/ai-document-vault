#!/usr/bin/env python3
"""Fail if a package in frontend/package-lock.json has a licence the project does not allow.

The policy is in CONTRIBUTING.md and is the same one `check_python_licenses.py` applies: permissive
licences only (no GPL, AGPL, SSPL or non-commercial), LGPL, MPL and custom licences case by case.
The lock file records an SPDX `license` for every package, so no install is needed. A package
without a readable licence fails; it is never assumed to be fine.

A package reviewed case by case is listed in REVIEWED below with the licence it was reviewed under
and why it is acceptable; the exception stops applying if the licence changes. `--strict` ignores
REVIEWED and checks only packages that are not development-only, so it fails the build if a
reviewed exception ever reaches what ships.

Standard library only. Usage (LOCKFILE defaults to frontend/package-lock.json):
    python scripts/check_npm_licenses.py [--strict] [LOCKFILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from check_python_licenses import expression_allowed

DEFAULT_LOCKFILE = Path(__file__).resolve().parent.parent / "frontend" / "package-lock.json"

# Reviewed case by case (CONTRIBUTING.md). Key: package name pattern. Value: the licence it was
# reviewed under, and why it is acceptable. All are build or test tools that are not shipped; the
# runtime-only `--strict` check runs without them.
REVIEWED: dict[str, tuple[str, str]] = {
    "@csstools/*": (
        "MIT-0",
        "MIT-0 is MIT without the attribution requirement: more permissive than the allowed MIT. "
        "Development-only (jsdom's CSS parsing).",
    ),
    "lru-cache": (
        "BlueOak-1.0.0",
        "Blue Oak Model License is a permissive licence in the same family as ISC. "
        "Development-only (ESLint and test tooling).",
    ),
    "minimatch": (
        "BlueOak-1.0.0",
        "Blue Oak Model License is a permissive licence in the same family as ISC. "
        "Development-only (ESLint and test tooling).",
    ),
    "caniuse-lite": (
        "CC-BY-4.0",
        "Browser-support data under an attribution-only licence. Development-only (Vite's "
        "browser targets); the data is not part of the built app.",
    ),
    "lightningcss*": (
        "MPL-2.0",
        "MPL-2.0 is file-level copyleft and applies only if lightningcss itself is modified or "
        "redistributed. It is a Vite build tool; its code is not in the built app.",
    ),
}


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    licence: str | None
    dev: bool


def read_lockfile(path: Path) -> list[Package]:
    lock = json.loads(path.read_text(encoding="utf-8"))
    packages = []
    for location, info in lock.get("packages", {}).items():
        if not location:  # the root project itself
            continue
        licence = info.get("license")
        packages.append(
            Package(
                name=location.rsplit("node_modules/", 1)[-1],
                version=str(info.get("version", "?")),
                licence=licence if isinstance(licence, str) else None,
                dev=info.get("dev") is True,
            )
        )
    return packages


def reviewed(package: Package) -> bool:
    return any(
        fnmatchcase(package.name, pattern) and package.licence == licence
        for pattern, (licence, _reason) in REVIEWED.items()
    )


def violations(packages: Iterable[Package], *, strict: bool = False) -> list[str]:
    problems = []
    for package in sorted(packages, key=lambda p: (p.name, p.version)):
        if strict and package.dev:
            continue
        if package.licence and expression_allowed(package.licence):
            continue
        if not strict and reviewed(package):
            continue
        found = package.licence or "no licence found"
        problems.append(f"{package.name} {package.version}: {found}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--strict", action="store_true", help="runtime packages, no exceptions")
    parser.add_argument("lockfile", nargs="?", type=Path, default=DEFAULT_LOCKFILE)
    args = parser.parse_args(argv)

    try:
        packages = read_lockfile(args.lockfile)
    except (OSError, json.JSONDecodeError) as error:
        print(f"cannot read {args.lockfile}: {type(error).__name__}", file=sys.stderr)
        return 2
    if not packages:
        print(f"no packages found in {args.lockfile}", file=sys.stderr)
        return 2
    problems = violations(packages, strict=args.strict)
    if problems:
        print("npm licence check failed (see CONTRIBUTING.md):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"npm licence check passed: {len(packages)} packages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
