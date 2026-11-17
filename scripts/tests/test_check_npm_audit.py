"""Tests for scripts/check_npm_audit.py (standard library only)."""

from __future__ import annotations

import sys
import unittest
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


def report(vulnerabilities: dict[str, list[object]]) -> dict:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {name: {"via": via} for name, via in vulnerabilities.items()},
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
    latest: str = "4.0.7",
    today: date = TODAY,
) -> tuple[list[str], list[str]]:
    return audit.check(data, lockfile, latest=lambda _name: latest, today=today)


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
                "node_modules/picomatch": {"dev": True},
                "node_modules/app/node_modules/picomatch": {},
            },
        }
        failures, _ = run(lockfile=runtime)
        self.assertIn("not development-only", failures[0])

    def test_a_package_missing_from_the_lock_fails(self) -> None:
        failures, _ = run(lockfile={"packages": {"node_modules/picomatchx": {"dev": True}}})
        self.assertIn("not in the lock file", failures[0])

    def test_a_published_fix_ends_the_exemption(self) -> None:
        failures, _ = run(latest="4.0.8")
        self.assertIn("outside the vulnerable range", failures[0])
        failures, _ = run(latest="5.0.0")
        self.assertIn("outside the vulnerable range", failures[0])

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

    def test_an_unreadable_latest_version_is_refused(self) -> None:
        for latest in ("4.0.8-beta.1", "v4.0.7", "4.0", "", "4.0.7\n", "04.0.7"):
            with self.subTest(latest=latest), self.assertRaises(audit.AuditError):
                run(latest=latest)

    def test_an_unreadable_lock_file_is_refused(self) -> None:
        for lockfile in (None, {}, {"packages": []}):
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

    def test_arguments_are_refused(self) -> None:
        with mock.patch("sys.stderr"):
            self.assertEqual(audit.main(["--omit=dev"]), 2)


if __name__ == "__main__":
    unittest.main()
