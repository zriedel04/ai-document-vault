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
        Notice("npm", "beta", "(MIT OR CC0-1.0)", (("LICENSE", "upstream text"),), "From upstream."),
    ]


class RenderTest(unittest.TestCase):
    def test_the_table_reads_back_what_was_rendered(self) -> None:
        text = notices.render(sample())
        self.assertEqual(
            notices.table(text, "npm"), {("@scope/alpha", "MIT"), ("beta", "(MIT OR CC0-1.0)")}
        )
        self.assertEqual(notices.table(text, "python"), {("zeta", "BSD-3-Clause")})

    def test_texts_are_included_and_a_reviewed_text_says_where_it_comes_from(self) -> None:
        text = notices.render(sample())
        self.assertIn("MIT text", text)
        self.assertIn("BSD text", text)
        beta = text.split("## npm: beta", 1)[1].split("## ", 1)[0]
        self.assertIn("From upstream.", beta)
        self.assertIn("upstream text", beta)

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

    def test_a_changed_licence_text_is_drift(self) -> None:
        """Codex #39: an update that keeps the licence but changes its notice needs regenerating."""
        current = [
            Notice(n.ecosystem, n.name, n.licence, (("LICENSE", "MIT text, 2027"),))
            if n.name == "@scope/alpha"
            else n
            for n in sample()
        ]
        self.assertEqual(
            notices.drift(self.committed, current, "npm"), ["~ @scope/alpha: licence text differs"]
        )

    def test_a_licence_file_added_or_removed_is_drift(self) -> None:
        current = [
            Notice(n.ecosystem, n.name, n.licence, (("LICENSE", "a new text"),), n.source)
            if n.name == "beta"
            else Notice(n.ecosystem, n.name, n.licence, ())
            if n.name == "@scope/alpha"
            else n
            for n in sample()
        ]
        self.assertEqual(
            notices.drift(self.committed, current, "npm"),
            ["~ @scope/alpha: licence text differs", "~ beta: licence text differs"],
        )

    def test_a_text_change_in_the_other_ecosystem_is_not_reported(self) -> None:
        current = [
            Notice(n.ecosystem, n.name, n.licence, (("LICENSE.txt", "new BSD text"),))
            if n.ecosystem == "python"
            else n
            for n in sample()
        ]
        self.assertEqual(notices.drift(self.committed, current, "npm"), [])
        self.assertEqual(
            notices.drift(self.committed, current, "python"), ["~ zeta: licence text differs"]
        )

    def test_a_deleted_notice_section_is_drift(self) -> None:
        """Codex #39: the table row alone is not the notice; its licence text must be there too."""
        committed = self.committed.replace("\n## npm: beta\n", "\n## npm: removed-by-hand\n")
        self.assertEqual(
            notices.drift(committed, sample(), "npm"),
            [
                "~ beta: notice section missing",
                "~ removed-by-hand: notice section for a package that is not listed",
            ],
        )
        without = self.committed.split("\n## npm: beta\n", 1)[0] + "\n"
        self.assertEqual(notices.drift(without, sample(), "npm"), ["~ beta: notice section missing"])

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
                "node_modules/@scope/pkg/node_modules/shipped": {"version": "0.9.0", "license": "MIT"},
                "node_modules/tool": {"version": "3.0.0", "license": "MIT", "dev": True},
            }
        }
        (folder / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        shipped = folder / "node_modules" / "shipped"
        shipped.mkdir(parents=True)
        (shipped / "LICENSE").write_text("shipped licence", encoding="utf-8")
        (shipped / "license.js").write_text("code, not a licence", encoding="utf-8")
        (shipped / "index.js").write_text("", encoding="utf-8")
        nested = folder / "node_modules" / "@scope" / "pkg" / "node_modules" / "shipped"
        nested.mkdir(parents=True)
        (nested / "LICENSE").write_text("older shipped licence", encoding="utf-8")
        return folder / "package-lock.json"

    def test_runtime_packages_with_their_licence_files_and_no_dev_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            found = {n.name: n for n in notices.npm_notices(self.build(folder), folder)}
        self.assertEqual(set(found), {"shipped", "@scope/pkg", "@scope/pkg/node_modules/shipped"})
        self.assertEqual(found["shipped"].texts, (("LICENSE", "shipped licence"),))
        self.assertEqual(
            found["@scope/pkg/node_modules/shipped"].texts, (("LICENSE", "older shipped licence"),)
        )
        self.assertEqual(found["@scope/pkg"].texts, ())
        self.assertEqual(found["@scope/pkg"].licence, "ISC")

    def test_a_package_that_is_not_installed_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            lockfile = self.build(folder)
            (folder / "node_modules" / "@scope" / "pkg" / "node_modules" / "shipped" / "LICENSE").unlink()
            (folder / "node_modules" / "@scope" / "pkg" / "node_modules" / "shipped").rmdir()
            with self.assertRaises(FileNotFoundError):
                notices.npm_notices(lockfile, folder)


class OverrideTest(unittest.TestCase):
    """Codex #39: a runtime package that ships no licence file still needs its text in the notices."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        (self.folder / "bare.LICENSE").write_text("reviewed text", encoding="utf-8")
        self.saved = dict(notices.OVERRIDES)
        notices.OVERRIDES.clear()
        notices.OVERRIDES["npm:bare"] = ("bare.LICENSE", "From the upstream repository.")

    def tearDown(self) -> None:
        notices.OVERRIDES.clear()
        notices.OVERRIDES.update(self.saved)
        self.tmp.cleanup()

    def test_the_reviewed_text_fills_the_gap(self) -> None:
        found = notices.with_overrides(
            [Notice("npm", "bare", "MIT", ()), Notice("npm", "full", "MIT", (("LICENSE", "x"),))],
            self.folder,
        )
        self.assertEqual(
            found[0], Notice("npm", "bare", "MIT", (("LICENSE", "reviewed text"),), "From the upstream repository.")
        )
        self.assertEqual(found[1], Notice("npm", "full", "MIT", (("LICENSE", "x"),)))

    def test_a_package_with_no_text_and_no_override_fails(self) -> None:
        with self.assertRaisesRegex(notices.NoticeError, "npm:other ships no licence file"):
            notices.with_overrides(
                [Notice("npm", "bare", "MIT", ()), Notice("npm", "other", "MIT", ())], self.folder
            )
        with self.assertRaisesRegex(notices.NoticeError, "python:zeta ships no licence file"):
            notices.with_overrides([Notice("python", "zeta", "MIT", ())], self.folder)

    def test_an_override_for_a_package_that_now_ships_a_file_fails(self) -> None:
        with self.assertRaisesRegex(notices.NoticeError, "now ships its own licence file"):
            notices.with_overrides([Notice("npm", "bare", "MIT", (("LICENSE", "x"),))], self.folder)

    def test_an_override_for_a_package_that_is_gone_fails_only_for_its_ecosystem(self) -> None:
        with self.assertRaisesRegex(notices.NoticeError, "not runtime packages: npm:bare"):
            notices.with_overrides([Notice("npm", "full", "MIT", (("LICENSE", "x"),))], self.folder)
        python_only = [Notice("python", "zeta", "MIT", (("LICENSE", "x"),))]
        self.assertEqual(notices.with_overrides(python_only, self.folder), python_only)

    def test_a_missing_reviewed_text_fails(self) -> None:
        (self.folder / "bare.LICENSE").unlink()
        with self.assertRaisesRegex(notices.NoticeError, "bare.LICENSE is missing"):
            notices.with_overrides([Notice("npm", "bare", "MIT", ())], self.folder)

    def test_every_committed_override_has_its_text(self) -> None:
        for key, (file_name, source) in self.saved.items():
            with self.subTest(key=key):
                self.assertTrue((notices.OVERRIDE_FOLDER / file_name).is_file())
                self.assertTrue(source.strip())


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
            (location.removeprefix("node_modules/"), info.get("license"))
            for location, info in lock["packages"].items()
            if location and info.get("dev") is not True
        }
        committed = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertEqual(notices.table(committed, "npm"), runtime)


if __name__ == "__main__":
    unittest.main()
