#!/usr/bin/env python3
"""Fail if an installed Python package has a licence the project does not allow.

The policy is in CONTRIBUTING.md: permissive licences only (no GPL, AGPL, SSPL or
non-commercial), LGPL, MPL and custom licences case by case. This script reads the licence
metadata of every package installed in the current environment, so run it with the backend's
interpreter (`uv run python ../scripts/check_python_licenses.py` from `backend/`).

A package passes if its SPDX expression (`License-Expression`), else its licence classifiers, else a
short `License` field names only allowed licences. A package whose licence cannot be read fails: it
is never assumed to be fine. A package reviewed case by case is listed in REVIEWED below with the
licence it was reviewed under and why it is acceptable; the exception stops applying if the
package's licence changes. `--strict` ignores REVIEWED and is used on the runtime-only environment,
so a reviewed exception can never reach what ships.

Standard library only. Usage:
    python scripts/check_python_licenses.py [--strict] [--ignore NAME ...]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions

ALLOWED = frozenset(
    spdx.lower()
    for spdx in (
        "MIT",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "Apache-2.0",
        "ISC",
        "PSF-2.0",
        "Python-2.0",
        "Unlicense",
        "0BSD",
        "Zlib",
        "CC0-1.0",
    )
)

# Licence names that older packages write in classifiers or the `License` field.
ALIASES = {
    "mit license": "MIT",
    "mit": "MIT",
    "bsd license": "BSD-3-Clause",
    "bsd": "BSD-3-Clause",
    "bsd-3-clause license": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "bsd-2-clause license": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "apache software license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2.0 license": "Apache-2.0",
    "isc license (iscl)": "ISC",
    "isc license": "ISC",
    "python software foundation license": "PSF-2.0",
    "the unlicense (unlicense)": "Unlicense",
    "zero-clause bsd (0bsd)": "0BSD",
    "zlib/libpng license": "Zlib",
    "cc0 1.0 universal (cc0 1.0) public domain dedication": "CC0-1.0",
}

# Reviewed case by case (CONTRIBUTING.md). Key: normalised package name. Value: the licence it was
# reviewed under, and why it is acceptable. Only for tools that are not part of the shipped app.
REVIEWED: dict[str, tuple[str, str]] = {
    "pathspec": (
        "Mozilla Public License 2.0 (MPL 2.0)",
        "MPL-2.0 is file-level copyleft and applies only if pathspec itself is modified or "
        "redistributed. It is a dependency of mypy, a development tool that is not shipped.",
    ),
    "pillow": (
        "MIT-CMU",
        "MIT-CMU (the HPND family) is a permissive licence that only asks for the copyright and "
        "permission notice to be kept. It is not yet on the allowed list. Pillow is used by the "
        "demo corpus generator in demo/generate, which is a development tool that is not shipped.",
    ),
}

CLASSIFIER_PREFIX = "License :: "
OSI = "OSI Approved :: "
MAX_FIELD_LENGTH = 100
TOKEN = re.compile(r"\(|\)|[^\s()]+")


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    expression: str | None
    classifiers: tuple[str, ...]
    field: str | None

    @property
    def description(self) -> str:
        found = self.expression or "; ".join(self.classifiers) or self.field or "no licence found"
        return found.replace("\n", " ")[:120]


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _evaluate(tokens: list[str]) -> bool:
    """Evaluate an SPDX expression: OR means any allowed, AND means all allowed."""
    position = 0
    malformed = False

    def operand() -> bool:
        nonlocal position, malformed
        if position >= len(tokens):
            malformed = True
            return False
        token = tokens[position]
        position += 1
        if token == "(":
            value = disjunction()
            if position < len(tokens) and tokens[position] == ")":
                position += 1
                return value
            malformed = True
            return False
        if token == ")" or token.upper() in {"AND", "OR", "WITH"}:
            malformed = True
            return False
        allowed = token.lower() in ALLOWED
        if position < len(tokens) and tokens[position].upper() == "WITH":
            position += 2  # an exception clause never makes a disallowed licence allowed
        return allowed

    def conjunction() -> bool:
        nonlocal position
        value = operand()
        while position < len(tokens) and tokens[position].upper() == "AND":
            position += 1
            value = operand() and value
        return value

    def disjunction() -> bool:
        nonlocal position
        value = conjunction()
        while position < len(tokens) and tokens[position].upper() == "OR":
            position += 1
            value = conjunction() or value
        return value

    result = disjunction()
    return result and not malformed and position == len(tokens)


def expression_allowed(expression: str) -> bool:
    return _evaluate(TOKEN.findall(expression))


def name_allowed(name: str) -> bool:
    """A licence written as a classifier or a plain `License` field."""
    cleaned = name.strip()
    if cleaned.startswith(CLASSIFIER_PREFIX):
        cleaned = cleaned[len(CLASSIFIER_PREFIX) :]
    cleaned = cleaned.removeprefix(OSI)
    key = cleaned.lower()
    return key in ALIASES or expression_allowed(cleaned)


def package_allowed(package: Package) -> bool:
    if package.expression:
        return expression_allowed(package.expression)
    if package.classifiers:
        return all(name_allowed(classifier) for classifier in package.classifiers)
    if package.field and len(package.field) <= MAX_FIELD_LENGTH and "\n" not in package.field:
        return name_allowed(package.field)
    return False


def read(distribution: Distribution) -> Package:
    meta = distribution.metadata
    classifiers = tuple(
        value.removeprefix(CLASSIFIER_PREFIX).removeprefix(OSI)
        for value in meta.get_all("Classifier") or []
        if value.startswith(CLASSIFIER_PREFIX)
    )
    return Package(
        name=meta["Name"] or "(unnamed)",
        version=meta["Version"] or "?",
        expression=meta.get("License-Expression"),
        classifiers=classifiers,
        field=meta.get("License"),
    )


def reviewed(package: Package) -> bool:
    entry = REVIEWED.get(normalise(package.name))
    if entry is None:
        return False
    licence = entry[0]
    return licence in package.classifiers or licence in (package.expression, package.field)


def violations(
    packages: Iterable[Package], *, strict: bool = False, ignore: Iterable[str] = ()
) -> list[str]:
    skipped = {normalise(name) for name in ignore}
    problems = []
    for package in sorted(packages, key=lambda p: normalise(p.name)):
        if normalise(package.name) in skipped or package_allowed(package):
            continue
        if not strict and reviewed(package):
            continue
        problems.append(f"{package.name} {package.version}: {package.description}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--strict", action="store_true", help="ignore the reviewed exceptions")
    parser.add_argument("--ignore", action="append", default=[], metavar="NAME")
    args = parser.parse_args(argv)

    packages = [read(distribution) for distribution in distributions()]
    problems = violations(packages, strict=args.strict, ignore=args.ignore)
    if problems:
        print("Licence check failed (see CONTRIBUTING.md):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"Licence check passed: {len(packages)} packages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
