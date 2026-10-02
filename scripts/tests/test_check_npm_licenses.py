"""Tests for scripts/check_npm_licenses.py (standard library only)."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import check_npm_licenses as npm  # noqa: E402


def package(
    name: str = "demo", licence: str | None = "MIT", *, dev: bool = False, version: str = "1.0.0"
) -> npm.Package:
    return npm.Package(name, version, licence, dev)


def lockfile(entries: dict[str, dict[str, object]]) -> str:
    return json.dumps(
        {"name": "x", "lockfileVersion": 3, "packages": {"": {"name": "x"}, **entries}}
    )


class ViolationsTest(unittest.TestCase):
    def test_allowed_licences_pass(self) -> None:
        for licence in ("MIT", "ISC", "Apache-2.0", "BSD-3-Clause", "(MIT OR CC0-1.0)", "0BSD"):
            with self.subTest(licence=licence):
                self.assertEqual(npm.violations([package(licence=licence)]), [])

    def test_disallowed_licences_fail(self) -> None:
        for licence in ("GPL-3.0-only", "AGPL-3.0-or-later", "LGPL-3.0", "SSPL-1.0", "UNLICENSED"):
            with self.subTest(licence=licence):
                self.assertEqual(len(npm.violations([package(licence=licence)])), 1)

    def test_missing_licence_fails(self) -> None:
        problems = npm.violations([package("mystery", None)])
        self.assertEqual(problems, ["mystery 1.0.0: no licence found"])

    def test_see_license_in_file_is_not_trusted(self) -> None:
        self.assertEqual(len(npm.violations([package(licence="SEE LICENSE IN LICENSE.txt")])), 1)

    def test_reviewed_exceptions_apply_only_outside_strict_mode(self) -> None:
        mpl = package("lightningcss-linux-x64-gnu", "MPL-2.0", dev=True)
        self.assertEqual(npm.violations([mpl]), [])
        runtime = package("lightningcss", "MPL-2.0")
        self.assertEqual(len(npm.violations([runtime], strict=True)), 1)

    def test_a_reviewed_exception_stops_when_the_licence_changes(self) -> None:
        changed = package("lightningcss", "GPL-3.0-only", dev=True)
        self.assertEqual(len(npm.violations([changed])), 1)

    def test_exceptions_are_per_package(self) -> None:
        other = package("some-other-tool", "MPL-2.0", dev=True)
        self.assertEqual(len(npm.violations([other])), 1)

    def test_strict_mode_skips_development_packages_only(self) -> None:
        packages = [package("tool", "GPL-3.0-only", dev=True), package("lib", "GPL-3.0-only")]
        problems = npm.violations(packages, strict=True)
        self.assertEqual(problems, ["lib 1.0.0: GPL-3.0-only"])


class LockfileTest(unittest.TestCase):
    def run_main(self, text: str, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "package-lock.json"
            path.write_text(text, encoding="utf-8")
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = npm.main([*args, str(path)])
        return code, err.getvalue()

    def test_reads_names_from_nested_paths_and_skips_the_root(self) -> None:
        text = lockfile(
            {
                "node_modules/@scope/pkg": {"version": "2.0.0", "license": "MIT"},
                "node_modules/a/node_modules/b": {
                    "version": "1.0.0",
                    "license": "ISC",
                    "dev": True,
                },
            }
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "package-lock.json"
            path.write_text(text, encoding="utf-8")
            packages = npm.read_lockfile(path)
        self.assertEqual(
            [(p.name, p.version, p.licence, p.dev) for p in packages],
            [("@scope/pkg", "2.0.0", "MIT", False), ("b", "1.0.0", "ISC", True)],
        )

    def test_fails_the_build_on_a_disallowed_licence(self) -> None:
        text = lockfile(
            {
                "node_modules/fine": {"version": "1.0.0", "license": "MIT"},
                "node_modules/viral": {"version": "3.1.0", "license": "AGPL-3.0-only"},
            }
        )
        code, err = self.run_main(text)
        self.assertEqual(code, 1)
        self.assertIn("viral 3.1.0: AGPL-3.0-only", err)
        self.assertNotIn("fine", err)

    def test_object_form_licence_is_treated_as_missing(self) -> None:
        text = lockfile({"node_modules/old": {"version": "1.0.0", "license": {"type": "MIT"}}})
        code, err = self.run_main(text)
        self.assertEqual(code, 1)
        self.assertIn("no licence found", err)

    def test_passes_when_every_package_is_allowed(self) -> None:
        text = lockfile({"node_modules/fine": {"version": "1.0.0", "license": "MIT"}})
        self.assertEqual(self.run_main(text)[0], 0)

    def test_unreadable_or_empty_lockfiles_are_errors_not_passes(self) -> None:
        self.assertEqual(self.run_main("not json")[0], 2)
        self.assertEqual(self.run_main(lockfile({}))[0], 2)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(npm.main([str(SCRIPTS / "does-not-exist.json")]), 2)

    def test_the_committed_lockfile_passes_both_modes(self) -> None:
        lock = SCRIPTS.parent / "frontend" / "package-lock.json"
        if not lock.is_file():
            self.skipTest("no frontend lock file")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(npm.main([str(lock)]), 0)
            self.assertEqual(npm.main(["--strict", str(lock)]), 0)


if __name__ == "__main__":
    unittest.main()
