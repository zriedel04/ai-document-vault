#!/usr/bin/env python3
"""Write, or check, THIRD_PARTY_NOTICES.md for the packages the app runs on.

The notices cover the runtime packages only: the npm packages the built frontend can include
(every package in `frontend/package-lock.json` that is not marked `dev`) and the Python packages
installed in a runtime-only backend environment (`uv sync --no-dev`). Build, test and lint tools
are not shipped and are covered by the licence audit (`docs/licence-audit.md`) instead.

An npm package that the lockfile marks `optional` and limits to some platforms (`os`, `cpu` or
`libc`) is a prebuilt native binary for Node, such as `@napi-rs/canvas-win32-x64-msvc` under
pdfjs-dist. npm installs only the one for the machine it runs on, and a browser build cannot
include a native binary, so these are left out: listing them would make the file depend on the
platform that wrote it. The package that loads such a binary (`@napi-rs/canvas`) has no platform
limit and is listed like any other. A platform-limited package that is not `optional` is read
like any other package, so it fails the run where it is not installed. Where such a binary is
installed, the run fails if it holds any file a bundler can take in (JavaScript, TypeScript, CSS,
WebAssembly), since then it is not only a binary.

Each package gets its name, licence and the licence text it ships with. An npm package installed
at more than one place gets an entry per place (`parent/node_modules/name`), since each copy is
code that may be bundled. A package that ships no licence file must have a reviewed text in
OVERRIDES, taken from its upstream source and stored in `scripts/notices/`; without one, `write`
and `check` fail, and an override for a package that now ships its own file fails too.

`write` needs both ecosystems in one place: run it from `backend/` with the runtime environment,
after `npm ci` in `frontend/`:

    uv sync --locked --no-dev
    uv run --no-sync python ../scripts/third_party_notices.py write --ignore vault
    uv sync --locked

`check npm` and `check python` compare the committed file for one ecosystem with what is installed
now: the package table (name and licence) and each package's licence texts. CI fails when a
runtime dependency is added, removed, relicensed or ships a changed licence text (a new copyright
line) without the notices being regenerated. The version is not compared, so a bump that keeps the
licence text needs no regeneration.

`check python` runs on Linux and on Windows, where the app runs, because a dependency can pull in
a package on one platform only (a `sys_platform == 'win32'` marker) or ship other licence files in
its Windows wheel. One does: pypdfium2's wheels ship the licences of the libraries built into
PDFium under a folder named for the platform (`data/linux_x64/BUILD_LICENSES/`,
`data/windows_x64/BUILD_LICENSES/`), with the same files and the same texts in each (checked for
5.14.0, apart from line endings, which `tidy` removes). PLATFORM_FOLDERS names such a folder per
package, and its label says `<platform>` in its place, so one file passes both checks. A wheel
whose platform folders hold different files or texts still fails one of the two checks, as it
should. A dependency that pulls in a package on one platform only, or ships other texts there,
still has no single file that passes both checks, since each compares the exact set of packages it
sees: CI then stays red until this script learns platform-specific entries (a platform column that
each check filters on, and a `write` that merges the two lists). That change belongs in the same PR
as the dependency that needs it.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from importlib.metadata import Distribution, distributions
from pathlib import Path

from check_python_licenses import licence_source, normalise, translated
from check_python_licenses import read as read_distribution

ROOT = Path(__file__).resolve().parent.parent
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"
LOCKFILE = ROOT / "frontend" / "package-lock.json"
NODE_MODULES = ROOT / "frontend"  # lockfile locations are relative to the frontend folder

# A file is notice material when its name says so (LICENSE, LICENCE.txt, LICENSE-MIT, LICENSE.BSD,
# COPYING.LESSER, NOTICE, COPYRIGHT, UNLICENSE, THIRD-PARTY-NOTICES.md...) or when it sits in a
# folder kept for licence texts (LICENSES/GPL-3.0.txt, and every file of a wheel's
# .dist-info/licenses/, which PEP 639 lets have any name).
LICENCE_NAME = re.compile(
    r"(?i)(un)?(licen[cs]es?|copying|notices?|copyrights?|copyrightnotice"
    r"|third[-_ ]?party[-_ ]?notices?)([-._ ][a-z0-9]+)*"
)
LICENCE_FOLDER = re.compile(
    r"(?i)licen[cs]es?|notices?|copying|copyrights?|third[-_ ]?party[-_ ]?notices?"
)
# What the last dotted part of such a file's name says. A text extension or a licence name in
# capitals (LICENSE.BSD, COPYING.LESSER, LICENSE.APACHE2) is a licence text; a known code, data or
# image extension is not (license.svg, license-url.test.ts). Anything else stops the run, so a new
# kind of file is classified by a person instead of being dropped from the notices unseen.
TEXT_SUFFIXES = frozenset({"txt", "text", "md", "markdown", "rst", "html", "htm"})
# A lower-case part that names the component the text belongs to, reviewed one by one: markdown-it-py
# ships the licence of the JavaScript markdown-it it ports as `licenses/LICENSE.markdown-it`.
# libjpeg-turbo's IJG licence comes as `README.ijg`, which pypdfium2 ships as `libjpeg_turbo.ijg`
# beside the libraries built into PDFium.
COMPONENT_SUFFIXES = frozenset({"markdown-it", "ijg"})
OTHER_SUFFIXES = frozenset(
    {
        *("js", "mjs", "cjs", "ts", "mts", "cts", "jsx", "tsx", "map", "css", "scss"),
        *("json", "yaml", "yml", "toml", "xml"),
        *("py", "pyi", "pyc", "pyd", "so", "dll", "dylib", "exe", "wasm"),
        *("svg", "png", "jpg", "jpeg", "gif", "ico", "webp"),
    }
)


ROW = re.compile(r"\| (npm|python) \| `([^`]+)` \| ([^|]+) \|")
ECOSYSTEMS = ("npm", "python")
OVERRIDE_FOLDER = Path(__file__).resolve().parent / "notices"

# Reviewed licence texts for runtime packages that ship none. Key: "<ecosystem>:<name as listed>".
# Value: the file in scripts/notices/ and where its text comes from.
OVERRIDES: dict[str, tuple[str, str]] = {
    "npm:react-remove-scroll-bar": (
        "react-remove-scroll-bar.LICENSE",
        "The package declares MIT but its tarball ships no licence file. This is the LICENSE its "
        "author added to the upstream repository (github.com/theKashey/react-remove-scroll-bar, "
        "commit 7301c16, 2025-05-21).",
    ),
}

HEADER = """# Third-party notices

This file is generated by `scripts/third_party_notices.py`; do not edit it by hand. It lists the
packages the AI Document Vault runs on, with the licence text each one ships. Development tools
that are not shipped are covered by the [licence audit](docs/licence-audit.md). The project's own
licence is in [LICENSE](LICENSE).
"""


def is_licence_file(path: str) -> bool:
    """Is the file at ``path`` (``/``-separated, inside a package or its RECORD) notice material?

    Raises ``NoticeError`` for a file that looks like one but whose kind is not classified above.
    """
    *folders, name = path.split("/")
    in_folder = any(LICENCE_FOLDER.fullmatch(part) for part in folders)
    if not (in_folder or LICENCE_NAME.fullmatch(name)):
        return False
    suffix = name.rpartition(".")[2] if "." in name.lstrip(".") else ""
    if not suffix or not any(c.islower() for c in suffix):
        return True
    if suffix.lower() in TEXT_SUFFIXES or suffix in COMPONENT_SUFFIXES:
        return True
    if suffix.lower() in OTHER_SUFFIXES:
        return False
    msg = f"{path} looks like a licence file of an unknown kind: classify .{suffix} in this script"
    raise NoticeError(msg)


class NoticeError(Exception):
    """A runtime package whose notice cannot be produced as the rules require."""


@dataclass(frozen=True)
class Notice:
    ecosystem: str
    name: str
    licence: str
    texts: tuple[tuple[str, str], ...]  # (file name, text)
    source: str = ""  # where the texts come from, when not from the package itself


def licence_files(folder: Path) -> tuple[tuple[str, str], ...]:
    """Every licence and notice file in a package ``folder``, at any depth, sorted by path.

    A package can ship a second licence for code it bundles (``vendor/LICENSE``), so the whole
    folder is read, not only its top level. Nested ``node_modules`` folders are skipped: each
    package installed there has an entry of its own. Each text is labelled with its path inside
    the package, so two files of the same name stay apart, and the order is the same on every
    platform.
    """
    found = []
    for path in folder.rglob("*"):
        inside = path.relative_to(folder)
        if "node_modules" in inside.parts[:-1] or not path.is_file():
            continue
        if is_licence_file(inside.as_posix()):
            found.append((inside.as_posix(), path.read_text(encoding="utf-8", errors="replace")))
    # Sorted by the label, not by Path: Windows compares paths without case, so sorting paths
    # would order the texts differently there and the file written on one platform would not
    # match the check on the other.
    return tuple(sorted(found))


PLATFORM_FIELDS = ("os", "cpu", "libc")


def is_platform_binary(info: dict[str, object]) -> bool:
    """Is this lockfile entry an optional package for some platforms only (see the module notes)?

    Both conditions are read as exactly their kind: ``optional`` must be ``true`` and a platform
    field a list, so a hand-edited ``"optional": "false"`` or ``"os": ""`` leaves the package in.
    """
    return info.get("optional") is True and any(
        isinstance(info.get(field), list) and info.get(field) for field in PLATFORM_FIELDS
    )


# Files a bundler can take into a browser build. A platform binary that ships one is refused.
BUNDLED_SUFFIXES = frozenset(
    {"js", "mjs", "cjs", "jsx", "ts", "mts", "cts", "tsx", "css", "scss", "wasm"}
)


def check_binary_only(location: str, folder: Path) -> None:
    """Fail if an installed platform binary holds a file a browser build could include."""
    for path in folder.rglob("*"):
        inside = path.relative_to(folder)
        if "node_modules" in inside.parts[:-1] or not path.is_file():
            continue
        if path.name.rpartition(".")[2].lower() in BUNDLED_SUFFIXES and "." in path.name:
            msg = (
                f"{location} is an optional platform package but ships {inside.as_posix()}, "
                "which a build could include: decide whether it belongs in the notices"
            )
            raise NoticeError(msg)


def npm_notices(lockfile: Path = LOCKFILE, base: Path = NODE_MODULES) -> list[Notice]:
    lock = json.loads(lockfile.read_text(encoding="utf-8"))
    notices = []
    for location, info in lock.get("packages", {}).items():
        if not location or info.get("dev") is True:
            continue
        if is_platform_binary(info):
            if (base / location).is_dir():
                check_binary_only(location, base / location)
            continue
        folder = base / location
        if not folder.is_dir():
            msg = f"{location} is not installed: run `npm ci` in frontend/ first"
            raise FileNotFoundError(msg)
        licence = info.get("license")
        notices.append(
            Notice(
                ecosystem="npm",
                name=location.removeprefix("node_modules/"),
                licence=licence if isinstance(licence, str) else "no licence found",
                texts=licence_files(folder),
            )
        )
    return notices


def python_licence(distribution: Distribution) -> str:
    """The licence the licence check judges: the SPDX expression, else the expression TRANSLATED
    gives for the package's exact licence field, else the classifiers or the field."""
    package = read_distribution(distribution)
    if package.expression:
        return package.expression
    if (expression := translated(package)) is not None:
        return expression
    return " AND ".join(licence_source(package)) or "no licence found"


# Packages whose wheels put licence files under a folder named for the platform (see the module
# notes), reviewed one by one: the normalised package name and the folder inside its metadata folder.
PLATFORM_FOLDERS: dict[str, re.Pattern[str]] = {
    "pypdfium2": re.compile(r"(?<=^licenses/data/)[a-z0-9]+_[a-z0-9]+(?=/BUILD_LICENSES/)"),
}


def python_label(path: str, name: str = "") -> str:
    """A licence file's label: its path inside the metadata folder (``licenses/LICENSE``), or,
    for a file the package installs elsewhere (a vendored library's ``pkg/_vendor/LICENSE``), its
    installed path. The metadata folder's own name holds the version, which would make every
    version bump look like a changed notice. For a package in PLATFORM_FOLDERS (``name``), the
    platform folder's name becomes ``<platform>``, so the label is the same on every platform."""
    if ".dist-info/" not in path:
        return path
    label = path.split(".dist-info/", 1)[1]
    folder = PLATFORM_FOLDERS.get(normalise(name))
    return folder.sub("<platform>", label, count=1) if folder else label


def read_installed(distribution: Distribution, path: str) -> str | None:
    """The text of an installed file listed in the distribution's RECORD."""
    located = Path(str(distribution.locate_file(path)))
    if not located.is_file():
        return None
    return located.read_text(encoding="utf-8", errors="replace")


def python_notices(
    ignore: Iterable[str] = (), installed: Iterable[Distribution] | None = None
) -> list[Notice]:
    """Each installed distribution (``installed`` in tests) with every licence file its RECORD
    lists, in its metadata folder or anywhere else it installs files."""
    skipped = {normalise(name) for name in ignore}
    notices = []
    for distribution in distributions() if installed is None else installed:
        name = distribution.metadata["Name"] or "(unnamed)"
        if normalise(name) in skipped:
            continue
        texts = tuple(
            (python_label(path, name), text)
            for path in sorted(str(f) for f in distribution.files or [])
            if is_licence_file(Path(path).as_posix())
            and (text := read_installed(distribution, path)) is not None
        )
        notices.append(Notice("python", name, python_licence(distribution), texts))
    return notices


def with_overrides(notices: Iterable[Notice], folder: Path = OVERRIDE_FOLDER) -> list[Notice]:
    """Fill in the reviewed text of every package that ships none; fail on any gap or stale entry."""
    result = []
    used = set()
    for shipped in notices:
        # A blank file (empty, or only white space) states no terms, so it is not a licence text:
        # a package whose files are all blank needs a reviewed text like one that ships none.
        notice = replace(shipped, texts=tuple(t for t in shipped.texts if t[1].strip()))
        key = f"{notice.ecosystem}:{notice.name}"
        override = OVERRIDES.get(key)
        if override is None:
            if not notice.texts:
                msg = f"{key} ships no licence file and has no reviewed text in OVERRIDES"
                raise NoticeError(msg)
            result.append(notice)
            continue
        used.add(key)
        if notice.texts:
            msg = f"{key} now ships its own licence file: remove its entry from OVERRIDES"
            raise NoticeError(msg)
        file_name, source = override
        path = folder / file_name
        if not path.is_file():
            msg = f"{key}: its reviewed text scripts/notices/{file_name} is missing"
            raise NoticeError(msg)
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            msg = f"{key}: its reviewed text scripts/notices/{file_name} is blank"
            raise NoticeError(msg)
        result.append(
            Notice(notice.ecosystem, notice.name, notice.licence, (("LICENSE", text),), source)
        )
    ecosystems = {notice.ecosystem for notice in result}
    unused = sorted(
        key for key in OVERRIDES if key not in used and key.split(":", 1)[0] in ecosystems
    )
    if unused:
        msg = f"OVERRIDES names packages that are not runtime packages: {', '.join(unused)}"
        raise NoticeError(msg)
    return result


def ordered(notices: Iterable[Notice]) -> list[Notice]:
    return sorted(notices, key=lambda n: (n.ecosystem, n.name.lower()))


def render(notices: Iterable[Notice]) -> str:
    items = ordered(notices)
    lines = [HEADER, "| Ecosystem | Package | Licence |", "| --- | --- | --- |"]
    lines += [f"| {n.ecosystem} | `{n.name}` | {n.licence} |" for n in items]
    for notice in items:
        lines += ["", f"## {notice.ecosystem}: {notice.name}", "", f"Licence: {notice.licence}"]
        if notice.source:
            lines += ["", notice.source]
        for file_name, text in notice.texts:
            fence = fence_for(text)
            lines += ["", f"`{file_name}`:", "", f"{fence}text", tidy(text), fence]
    return "\n".join(lines) + "\n"


def tidy(text: str) -> str:
    """The text without white space at the end of its lines or of the whole.

    The repository's trailing-whitespace hook strips it from every committed file, so a text
    written with it (pdfjs-dist's OpenJPEG licence has such lines) would never match the check
    after a commit. White space at the end of a line carries no terms.
    """
    return "\n".join(line.rstrip() for line in text.rstrip().split("\n"))


def fence_for(text: str) -> str:
    """A code fence longer than any run of backticks in ``text``, so no line of it closes the block."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def split(text: str) -> tuple[str, list[tuple[str, str]]]:
    """The part before the first package heading, and each (heading, body) after it.

    Only a ``## `` line outside a code fence is a heading: a licence text can hold a Markdown
    heading of its own (``## Warranty``), and splitting there would hide the rest of the text from
    the comparison.
    """
    preamble: list[str] = []
    found: list[tuple[str, list[str]]] = []
    fence = ""
    for line in text.split("\n"):
        if fence:
            if line == fence:
                fence = ""
        elif opening := re.fullmatch(r"(`{3,})text", line):
            fence = opening.group(1)
        elif line.startswith("## "):
            found.append((line.removeprefix("## "), []))
            continue
        (found[-1][1] if found else preamble).append(line)
    return "\n".join(preamble), [(heading, "\n".join(body).strip()) for heading, body in found]


def table(text: str, ecosystem: str) -> set[tuple[str, str]]:
    """The package table's rows for one ecosystem; the licence texts below it are not read."""
    text = split(text)[0]
    return {
        (name, licence.strip())
        for found_ecosystem, name, licence in ROW.findall(text)
        if found_ecosystem == ecosystem
    }


def sections(text: str, ecosystem: str) -> dict[str, str]:
    """Each package's section (licence and texts) for one ecosystem, by package name."""
    found = {}
    prefix = f"{ecosystem}: "
    for heading, body in split(text)[1]:
        if heading.startswith(prefix):
            found[heading.removeprefix(prefix)] = body
    return found


def drift(committed: str, notices: Iterable[Notice], ecosystem: str) -> list[str]:
    """What the committed file lacks (+), has too many (-) or holds an old text for (~).

    The version is not compared, so a version bump that keeps the licence and its text needs no
    regeneration; a changed licence text (a new copyright line) does.
    """
    mine = [n for n in notices if n.ecosystem == ecosystem]
    current = {(n.name, n.licence) for n in mine}
    listed = table(committed, ecosystem)
    problems = [f"+ {name}: {licence}" for name, licence in sorted(current - listed)] + [
        f"- {name}: {licence}" for name, licence in sorted(listed - current)
    ]
    reported = {name for name, _licence in current ^ listed}  # a changed licence says it already
    written = sections(committed, ecosystem)
    expected = sections(render(mine), ecosystem)
    for name in sorted(expected.keys() | written.keys()):
        if name in reported:
            continue
        if name not in written:
            problems.append(f"~ {name}: notice section missing")
        elif name not in expected:
            problems.append(f"~ {name}: notice section for a package that is not listed")
        elif written[name] != expected[name]:
            problems.append(f"~ {name}: licence text differs")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("write", help="regenerate THIRD_PARTY_NOTICES.md")
    write.add_argument("--ignore", action="append", default=[], metavar="NAME")
    check = commands.add_parser("check", help="fail if the package table is out of date")
    check.add_argument("ecosystem", choices=ECOSYSTEMS)
    check.add_argument("--ignore", action="append", default=[], metavar="NAME")
    args = parser.parse_args(argv)

    try:
        if args.command == "write":
            notices = with_overrides(npm_notices() + python_notices(args.ignore))
        elif args.ecosystem == "npm":
            notices = with_overrides(npm_notices())
        else:
            notices = with_overrides(python_notices(args.ignore))
    except NoticeError as error:
        print(f"cannot produce the notices: {error}", file=sys.stderr)
        return 1

    if args.command == "write":
        NOTICES.write_text(render(notices), encoding="utf-8", newline="\n")
        print(f"Wrote {NOTICES.name}: {len(notices)} packages.")
        return 0

    if not notices:
        print(f"no {args.ecosystem} packages found", file=sys.stderr)
        return 2
    problems = drift(NOTICES.read_text(encoding="utf-8"), notices, args.ecosystem)
    if problems:
        print(
            f"{NOTICES.name} is out of date for {args.ecosystem} "
            "(regenerate it, see scripts/third_party_notices.py):",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"{NOTICES.name} lists every {args.ecosystem} runtime package ({len(notices)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
