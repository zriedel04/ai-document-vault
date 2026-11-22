"""Tests for scripts/check_npm_audit.py (standard library only)."""

from __future__ import annotations

import subprocess
import sys
import unittest
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import check_npm_audit as audit  # noqa: E402

GHSA = "GHSA-fc5q-39xc-vjpf"
OTHER = "GHSA-2222-3333-4444"
TODAY = date(2026, 11, 17)


def advisory(ghsa: str = GHSA, name: str = "picomatch", vulnerable: str = "<=4.0.7") -> dict:
    return {
        "source": 1,
        "name": name,
        "url": f"https://github.com/advisories/{ghsa}",
        "severity": "high",
        "range": vulnerable,
    }


def report(vulnerabilities: dict[str, list[object]], **nodes: list[str]) -> dict:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            name: {"via": via, "nodes": nodes.get(name, [f"node_modules/{name}"])}
            for name, via in vulnerabilities.items()
        },
    }


DEV_LOCK = {
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "x"},
        "node_modules/picomatch": {"version": "4.0.7", "dev": True},
        "node_modules/vite/node_modules/picomatch": {"version": "4.0.7", "dev": True},
    },
}
# The report npm gave on 2026-11-17, cut to its shape: one advisory and the packages above it.
REAL = report(
    {
        "picomatch": [advisory()],
        "fdir": ["picomatch"],
        "tinyglobby": ["fdir", "picomatch"],
        "vite": ["picomatch", "tinyglobby"],
        "vitest": ["picomatch", "tinyglobby", "vite"],
    }
)


def run(
    data: object = REAL,
    lockfile: object = DEV_LOCK,
    *,
    published: Sequence[str] = ("2.3.1", "3.0.1", "4.0.6", "4.0.7"),
    today: date = TODAY,
) -> tuple[list[str], list[str]]:
    return audit.check(data, lockfile, published=lambda _name: list(published), today=today)


class Exemption(unittest.TestCase):
    def test_the_reviewed_advisory_is_excused_with_the_packages_above_it(self) -> None:
        failures, notices = run()
        self.assertEqual(failures, [])
        self.assertEqual(len(notices), 1)
        self.assertIn(GHSA, notices[0])

    def test_no_advisory_passes(self) -> None:
        self.assertEqual(run(report({})), ([], []))

    def test_an_unlisted_advisory_fails(self) -> None:
        failures, _ = run(report({"left-pad": [advisory(OTHER, "left-pad", "<9.0.0")]}))
        self.assertEqual(len(failures), 1)
        self.assertIn("not a reviewed exemption", failures[0])

    def test_an_unlisted_advisory_beside_the_exempt_one_fails(self) -> None:
        data = report(
            {
                "picomatch": [advisory()],
                "vite": ["picomatch", advisory(OTHER, "vite", "<99.0.0")],
            }
        )
        failures, _ = run(data)
        self.assertEqual(len(failures), 1)
        self.assertIn(OTHER, failures[0])

    def test_the_exemption_is_for_one_package(self) -> None:
        failures, _ = run(report({"other": [advisory(name="other")]}))
        self.assertIn("is for picomatch", failures[0])

    def test_it_expires(self) -> None:
        expires = audit.EXEMPTIONS[GHSA].expires
        self.assertEqual(run(today=expires)[0], [])
        failures, _ = run(today=date.fromordinal(expires.toordinal() + 1))
        self.assertIn("expired", failures[0])

    def test_a_runtime_copy_fails(self) -> None:
        runtime = {
            "lockfileVersion": 3,
            "packages": {
                "node_modules/picomatch": {"version": "4.0.7", "dev": True},
                "node_modules/app/node_modules/picomatch": {},
            },
        }
        failures, _ = run(lockfile=runtime)
        self.assertIn("not development-only", failures[0])

    def test_an_aliased_runtime_copy_fails(self) -> None:
        aliased = {
            "lockfileVersion": 3,
            "packages": DEV_LOCK["packages"]
            | {"node_modules/runtime-glob": {"name": "picomatch", "version": "4.0.7"}},
        }
        failures, _ = run(lockfile=aliased)
        self.assertIn("not development-only", failures[0])

    def test_an_aliased_dev_copy_is_a_copy_too(self) -> None:
        aliased = {
            "lockfileVersion": 3,
            "packages": {"node_modules/glob": {"name": "picomatch", "version": "4.0.7", "dev": True}},
        }
        data = report({"picomatch": [advisory()]}, picomatch=["node_modules/glob"])
        self.assertEqual(run(data, aliased)[0], [])

    def test_a_folder_named_like_the_package_that_installs_another_is_not_a_copy(self) -> None:
        renamed = {
            "lockfileVersion": 3,
            "packages": {
                "node_modules/picomatch": {"version": "4.0.7", "dev": True},
                "node_modules/a/node_modules/picomatch": {"name": "other"},
            },
        }
        self.assertEqual(run(report({"picomatch": [advisory()]}), renamed)[0], [])

    def test_every_path_npm_reports_must_be_a_listed_copy(self) -> None:
        data = report(
            {"picomatch": [advisory()]},
            picomatch=["node_modules/picomatch", "node_modules/runtime-glob"],
        )
        failures, _ = run(data)
        self.assertIn("does not list as this package", failures[0])

    def test_the_project_itself_is_not_a_copy(self) -> None:
        own = {"packages": {"": {"name": "picomatch"}, "node_modules/picomatch": {"version": "4.0.7", "dev": True}}}
        self.assertEqual(run(report({"picomatch": [advisory()]}), own)[0], [])

    def test_a_package_missing_from_the_lock_fails(self) -> None:
        failures, _ = run(lockfile={"packages": {"node_modules/picomatchx": {"dev": True}}})
        self.assertIn("not in the lock file", failures[0])

    def test_a_published_fix_ends_the_exemption(self) -> None:
        for fix in ("4.0.8", "5.0.0"):
            with self.subTest(fix=fix):
                failures, _ = run(published=("4.0.7", fix))
                self.assertIn(f"version {fix} is outside the vulnerable range", failures[0])

    def test_a_fix_under_another_tag_ends_the_exemption(self) -> None:
        # `latest` can still point at 4.0.7 while a fix was published with `--tag`; the order of
        # the registry's list says nothing about tags, so every listed release is judged.
        failures, _ = run(published=("4.0.8", "4.0.7"))
        self.assertIn("version 4.0.8 is outside", failures[0])

    def test_an_older_release_outside_the_range_is_not_a_fix(self) -> None:
        # Releases older than the installed copy would be a downgrade, not a fix.
        data = report({"picomatch": [advisory(vulnerable=">=2.0.0 <=4.0.7")]})
        self.assertEqual(run(data, published=("1.0.0", "4.0.7"))[0], [])

    def test_a_fix_newer_than_any_one_copy_ends_the_exemption(self) -> None:
        lock = {
            "packages": {
                "node_modules/picomatch": {"version": "4.0.7", "dev": True},
                "node_modules/a/node_modules/picomatch": {"version": "1.0.0", "dev": True},
            }
        }
        item = advisory(vulnerable="<=1.0.0 || >=4.0.0 <=4.0.7")
        data = report({"picomatch": [item]}, picomatch=list(lock["packages"]))
        failures, _ = run(data, lock, published=("1.0.0", "1.0.1", "4.0.7"))
        self.assertIn("version 1.0.1 is outside", failures[0])

    def test_a_prerelease_is_not_a_fix(self) -> None:
        for version in ("4.0.8-beta.1", "5.0.0-rc.0+build.7"):
            with self.subTest(version=version):
                self.assertEqual(run(published=("4.0.7", version))[0], [])

    def test_a_package_above_an_unexcused_one_fails_too(self) -> None:
        data = report({"picomatch": [advisory()], "a": ["b"], "b": ["a"]})
        failures, _ = run(data)
        self.assertEqual(sorted(f.split(":")[0] for f in failures), ["a", "b"])


class Unreadable(unittest.TestCase):
    def test_reports_of_another_shape_are_refused(self) -> None:
        for data in (
            None,
            [],
            {"error": {"code": "ENOAUDIT"}},
            {"auditReportVersion": 1, "vulnerabilities": {}},
            {"auditReportVersion": 2},
            report({"x": []}),
            report({"x": [42]}),
            report({"x": ["not-listed"]}),
            report({"x": [{"url": f"https://github.com/advisories/{GHSA}"}]}),
            report({"picomatch": [advisory()]}, picomatch=[]),
            {"auditReportVersion": 2, "vulnerabilities": {"picomatch": {"via": [advisory()]}}},
            report({"picomatch": [advisory()]}, picomatch=[7]),
            report({"other": [advisory()]}),
        ):
            with self.subTest(data=data), self.assertRaises(audit.AuditError):
                run(data)

    def test_only_github_advisory_links_are_read(self) -> None:
        for url in (
            f"http://github.com/advisories/{GHSA}",
            f"https://github.com/advisories/{GHSA}\n",
            f"https://github.com/advisories/{GHSA}/x",
            "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
            "https://npmjs.com/advisories/1",
        ):
            item = advisory() | {"url": url}
            with self.subTest(url=url), self.assertRaises(audit.AuditError):
                run(report({"picomatch": [item]}))

    def test_an_unreadable_published_version_is_refused(self) -> None:
        for version in ("v4.0.7", "4.0", "", "4.0.7\n", "04.0.7", "4.0.8-", "4.0.8-beta\n"):
            with self.subTest(version=version), self.assertRaises(audit.AuditError):
                run(published=("4.0.7", version))
        with self.assertRaises(audit.AuditError):
            run(published=())

    def test_a_copy_without_a_release_version_is_refused(self) -> None:
        for info in ({"dev": True}, {"version": 4, "dev": True}, {"version": "4.0", "dev": True}):
            lock = {"packages": {"node_modules/picomatch": info}}
            data = report({"picomatch": [advisory()]})
            with self.subTest(info=info), self.assertRaises(audit.AuditError):
                run(data, lock)

    def test_an_unreadable_lock_file_is_refused(self) -> None:
        for lockfile in (
            None,
            {},
            {"packages": []},
            {"packages": {"node_modules/picomatch": {"name": 7, "dev": True}}},
            {"packages": {"node_modules/picomatch": {"name": "", "dev": True}}},
        ):
            with self.subTest(lockfile=lockfile), self.assertRaises(audit.AuditError):
                run(lockfile=lockfile)


class Ranges(unittest.TestCase):
    def test_comparators(self) -> None:
        cases = [
            ("4.0.7", "<=4.0.7", True),
            ("4.0.8", "<=4.0.7", False),
            ("4.0.7", "<4.0.7", False),
            ("4.0.6", "<4.0.7", True),
            ("4.0.7", ">=4.0.0 <4.0.8", True),
            ("3.9.9", ">=4.0.0 <4.0.8", False),
            ("2.3.1", "<2.3.2 || >=4.0.0 <4.0.4", True),
            ("3.0.0", "<2.3.2 || >=4.0.0 <4.0.4", False),
            ("10.0.0", "*", True),
            ("1.2.3", "1.2.3", True),
            ("1.2.3", "=1.2.4", False),
            ("1.10.0", ">1.9.0", True),
        ]
        for version, spec, expected in cases:
            with self.subTest(version=version, spec=spec):
                self.assertIs(audit.in_range(version, spec), expected)

    def test_ranges_it_cannot_read_are_refused(self) -> None:
        for spec in ("", "||", "<=4.0.7-beta.0", "~4.0.0", "^4.0.0", "4.x", "<= 4.0.7", "!4.0.0"):
            with self.subTest(spec=spec), self.assertRaises(audit.AuditError):
                audit.in_range("4.0.7", spec)


class Main(unittest.TestCase):
    def test_a_run_that_cannot_read_npm_fails(self) -> None:
        with (
            mock.patch.object(audit, "_npm", side_effect=audit.AuditError("npm is not installed")),
            mock.patch("sys.stderr"),
        ):
            self.assertEqual(audit.main([]), 1)

    def test_every_published_version_is_asked_for(self) -> None:
        answers = {
            '["4.0.7", "4.0.8"]': ["4.0.7", "4.0.8"],
            '"4.0.7"': ["4.0.7"],
        }
        for stdout, expected in answers.items():
            done = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
            with self.subTest(stdout=stdout), mock.patch.object(audit, "_npm", return_value=done) as npm:
                self.assertEqual(audit.npm_published("picomatch"), expected)
                npm.assert_called_once_with("view", "picomatch", "versions", "--json")

    def test_an_unreadable_version_list_is_refused(self) -> None:
        for code, stdout in ((1, '["4.0.7"]'), (0, "{}"), (0, "[4]"), (0, "null")):
            done = subprocess.CompletedProcess([], code, stdout=stdout, stderr="")
            with (
                self.subTest(stdout=stdout, code=code),
                mock.patch.object(audit, "_npm", return_value=done),
                self.assertRaises(audit.AuditError),
            ):
                audit.npm_published("picomatch")

    def test_arguments_are_refused(self) -> None:
        with mock.patch("sys.stderr"):
            self.assertEqual(audit.main(["--omit=dev"]), 2)


if __name__ == "__main__":
    unittest.main()
