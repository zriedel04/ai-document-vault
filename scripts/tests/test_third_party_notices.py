"""Tests for scripts/third_party_notices.py (standard library only, synthetic packages)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import third_party_notices as notices  # noqa: E402
from third_party_notices import Notice  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def sample() -> list[Notice]:
    return [
        Notice("python", "zeta", "BSD-3-Clause", (("LICENSE.txt", "BSD text"),)),
        Notice("npm", "@scope/alpha", "MIT", (("LICENSE", "MIT text"),)),
        Notice("npm", "beta", "(MIT OR CC0-1.0)", ()),
    ]


class RenderTest(unittest.TestCase):
    def test_the_table_reads_back_what_was_rendered(self) -> None:
        text = notices.render(sample())
        self.assertEqual(
            notices.table(text, "npm"), {("@scope/alpha", "MIT"), ("beta", "(MIT OR CC0-1.0)")}
        )
        self.assertEqual(notices.table(text, "python"), {("zeta", "BSD-3-Clause")})

    def test_texts_are_included_and_a_missing_one_is_said_not_invented(self) -> None:
        text = notices.render(sample())
        self.assertIn("MIT text", text)
        self.assertIn("BSD text", text)
        beta = text.split("## npm: beta", 1)[1].split("## ", 1)[0]
        self.assertIn("ships no licence file", beta)

    def test_a_row_inside_a_licence_text_is_not_read_as_a_package(self) -> None:
        text = notices.render([Notice("npm", "real", "MIT", (("LICENSE", "| npm | `fake` | MIT |"),))])
        self.assertEqual(notices.table(text, "npm"), {("real", "MIT")})

    def test_a_text_holding_a_code_fence_does_not_end_the_block_early(self) -> None:
        text = notices.render([Notice("npm", "fenced", "MIT", (("LICENSE", "a\n```\nb"),))])
        self.assertIn("````text\na\n```\nb\n````", text)


class DriftTest(unittest.TestCase):
    committed = notices.render(sample())

    def test_no_drift_when_nothing_changed(self) -> None:
        self.assertEqual(notices.drift(self.committed, sample(), "npm"), [])
        self.assertEqual(notices.drift(self.committed, sample(), "python"), [])

    def test_a_new_package_a_removed_one_and_a_relicensed_one_are_reported(self) -> None:
        current = [
            Notice("npm", "@scope/alpha", "Apache-2.0", ()),  # relicensed
            Notice("npm", "gamma", "ISC", ()),  # added; beta removed
        ]
        self.assertEqual(
            notices.drift(self.committed, current, "npm"),
            [
                "+ @scope/alpha: Apache-2.0",
                "+ gamma: ISC",
                "- @scope/alpha: MIT",
                "- beta: (MIT OR CC0-1.0)",
            ],
        )

    def test_a_new_licence_text_alone_is_not_drift(self) -> None:
        current = [Notice(n.ecosystem, n.name, n.licence, (("LICENSE", "new"),)) for n in sample()]
        self.assertEqual(notices.drift(self.committed, current, "npm"), [])

    def test_one_ecosystem_is_compared_without_the_other(self) -> None:
        only_npm = [n for n in sample() if n.ecosystem == "npm"]
        self.assertEqual(notices.drift(self.committed, only_npm, "npm"), [])


class NpmTest(unittest.TestCase):
    def build(self, folder: Path) -> Path:
        lock = {
            "packages": {
                "": {"name": "app"},
                "node_modules/shipped": {"version": "1.0.0", "license": "MIT"},
                "node_modules/@scope/pkg": {"version": "2.0.0", "license": "ISC"},
                "node_modules/tool": {"version": "3.0.0", "license": "MIT", "dev": True},
            }
        }
        (folder / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        shipped = folder / "node_modules" / "shipped"
        shipped.mkdir(parents=True)
        (shipped / "LICENSE").write_text("shipped licence", encoding="utf-8")
        (shipped / "license.js").write_text("code, not a licence", encoding="utf-8")
        (shipped / "index.js").write_text("", encoding="utf-8")
        (folder / "node_modules" / "@scope" / "pkg").mkdir(parents=True)
        return folder / "package-lock.json"

    def test_runtime_packages_with_their_licence_files_and_no_dev_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            found = {n.name: n for n in notices.npm_notices(self.build(folder), folder)}
        self.assertEqual(set(found), {"shipped", "@scope/pkg"})
        self.assertEqual(found["shipped"].texts, (("LICENSE", "shipped licence"),))
        self.assertEqual(found["@scope/pkg"].texts, ())
        self.assertEqual(found["@scope/pkg"].licence, "ISC")

    def test_a_package_that_is_not_installed_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            lockfile = self.build(folder)
            (folder / "node_modules" / "@scope" / "pkg").rmdir()
            with self.assertRaises(FileNotFoundError):
                notices.npm_notices(lockfile, folder)


class LicenceFileTest(unittest.TestCase):
    def test_licence_file_names(self) -> None:
        for name in (
            "LICENSE",
            "LICENCE",
            "license",
            "LICENSE.md",
            "LICENSE.txt",
            "license-mit",
            "license-cc0",
            "COPYING",
            "NOTICE",
            "CopyrightNotice.txt",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(notices.LICENCE_FILE.fullmatch(name))
        for name in ("license.js", "licenses.json", "LICENSE.html", "notice.ts", "AUTHORS"):
            with self.subTest(name=name):
                self.assertIsNone(notices.LICENCE_FILE.fullmatch(name))


class CommittedFileTest(unittest.TestCase):
    def test_the_committed_npm_table_matches_the_lockfile(self) -> None:
        """The frontend lockfile is in the repository, so this check needs no installed packages."""
        lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
        runtime = {
            (location.rsplit("node_modules/", 1)[-1], info.get("license"))
            for location, info in lock["packages"].items()
            if location and info.get("dev") is not True
        }
        committed = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertEqual(notices.table(committed, "npm"), runtime)


if __name__ == "__main__":
    unittest.main()
