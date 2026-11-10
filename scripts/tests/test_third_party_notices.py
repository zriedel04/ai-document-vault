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


class SplitTest(unittest.TestCase):
    def test_a_heading_inside_a_licence_text_is_not_a_section(self) -> None:
        notice = Notice("npm", "headed", "MIT", (("LICENSE", "terms\n## Warranty\nold"),))
        [(heading, body)] = notices.split(notices.render([notice]))[1]
        self.assertEqual(heading, "npm: headed")
        self.assertIn("## Warranty\nold", body)

    def test_a_text_with_long_backtick_runs_gets_a_longer_fence(self) -> None:
        for text, fence in (("plain", "```"), ("a ``` b", "````"), ("````\n## x", "`````")):
            with self.subTest(text=text):
                self.assertEqual(notices.fence_for(text), fence)
                rendered = notices.render([Notice("npm", "p", "MIT", (("LICENSE", text),))])
                self.assertEqual([h for h, _ in notices.split(rendered)[1]], ["npm: p"])


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

    def test_a_change_after_a_heading_inside_a_licence_text_is_drift(self) -> None:
        """Codex #39: a `## ` line inside a fenced text must not end the comparison there."""
        before = [Notice("npm", "headed", "MIT", (("LICENSE", "terms\n## Warranty\nold"),))]
        after = [Notice("npm", "headed", "MIT", (("LICENSE", "terms\n## Warranty\nnew"),))]
        self.assertEqual(
            notices.drift(notices.render(before), after, "npm"),
            ["~ headed: licence text differs"],
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
        # Code the package bundles under its own licence, at any depth (Codex #39).
        (shipped / "vendor" / "gyp").mkdir(parents=True)
        (shipped / "vendor" / "LICENSE").write_text("vendored licence", encoding="utf-8")
        (shipped / "vendor" / "gyp" / "NOTICE.md").write_text("gyp notice", encoding="utf-8")
        # A licence folder and a copyright file count too; an icon named after a licence does not.
        (shipped / "LICENSES").mkdir()
        (shipped / "LICENSES" / "Zlib.txt").write_text("bundled zlib terms", encoding="utf-8")
        (shipped / "COPYRIGHT").write_text("attribution", encoding="utf-8")
        (shipped / "icons").mkdir()
        (shipped / "icons" / "license.svg").write_text("<svg/>", encoding="utf-8")
        # A copy installed below the package is an entry of its own, never part of this one.
        stray = shipped / "vendor" / "node_modules" / "other"
        stray.mkdir(parents=True)
        (stray / "LICENSE").write_text("another package's licence", encoding="utf-8")
        nested = folder / "node_modules" / "@scope" / "pkg" / "node_modules" / "shipped"
        nested.mkdir(parents=True)
        (nested / "LICENSE").write_text("older shipped licence", encoding="utf-8")
        return folder / "package-lock.json"

    def test_runtime_packages_with_their_licence_files_and_no_dev_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            found = {n.name: n for n in notices.npm_notices(self.build(folder), folder)}
        self.assertEqual(set(found), {"shipped", "@scope/pkg", "@scope/pkg/node_modules/shipped"})
        self.assertEqual(
            found["shipped"].texts,
            (
                ("COPYRIGHT", "attribution"),
                ("LICENSE", "shipped licence"),
                ("LICENSES/Zlib.txt", "bundled zlib terms"),
                ("vendor/LICENSE", "vendored licence"),
                ("vendor/gyp/NOTICE.md", "gyp notice"),
            ),
        )
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


class PythonTest(unittest.TestCase):
    """Codex #39: a wheel can install a vendored library's licence outside its metadata folder."""

    def test_every_licence_file_in_the_record_with_a_label_that_has_no_version(self) -> None:
        from importlib.metadata import PathDistribution

        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            meta = site / "pkg-1.2.3.dist-info"
            (meta / "licenses").mkdir(parents=True)
            (site / "pkg" / "_vendor" / "lib").mkdir(parents=True)
            (meta / "METADATA").write_text(
                "Metadata-Version: 2.4\nName: pkg\nVersion: 1.2.3\nLicense-Expression: MIT\n",
                encoding="utf-8",
            )
            files = {
                "pkg-1.2.3.dist-info/licenses/LICENSE": "pkg licence",
                # PEP 639: every file in the licenses folder is a licence file, whatever its name.
                "pkg-1.2.3.dist-info/licenses/AUTHORS.rst": "pkg authors",
                "pkg/_vendor/lib/LICENSE.txt": "vendored licence",
                "pkg/__init__.py": "",
                "pkg/license.py": "code, not a licence",
            }
            for path, text in files.items():
                (site / path).write_text(text, encoding="utf-8")
            record = [*files, "pkg-1.2.3.dist-info/METADATA", "pkg-1.2.3.dist-info/RECORD"]
            (meta / "RECORD").write_text("".join(f"{p},,\n" for p in record), encoding="utf-8")

            [found] = notices.python_notices(installed=[PathDistribution(meta)])
        self.assertEqual(found.licence, "MIT")
        self.assertEqual(
            found.texts,
            (
                ("licenses/AUTHORS.rst", "pkg authors"),
                ("licenses/LICENSE", "pkg licence"),
                ("pkg/_vendor/lib/LICENSE.txt", "vendored licence"),
            ),
        )


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

    def test_a_blank_reviewed_text_fails(self) -> None:
        for blank in ("", " \n\t\r\n"):
            (self.folder / "bare.LICENSE").write_text(blank, encoding="utf-8")
            with self.assertRaisesRegex(notices.NoticeError, "bare.LICENSE is blank"):
                notices.with_overrides([Notice("npm", "bare", "MIT", ())], self.folder)

    def test_a_package_whose_licence_files_are_all_blank_has_no_text(self) -> None:
        """Codex #39: an empty LICENSE states no terms, so it cannot stand for the licence."""
        blank = Notice("npm", "hollow", "MIT", (("LICENSE", ""), ("NOTICE", "  \n")))
        with self.assertRaisesRegex(notices.NoticeError, "npm:hollow ships no licence file"):
            notices.with_overrides([blank], self.folder)

    def test_a_blank_file_beside_a_real_text_is_left_out(self) -> None:
        mixed = Notice("npm", "mixed", "MIT", (("LICENSE", "MIT text"), ("NOTICE", "\n")))
        [kept] = notices.with_overrides([mixed, Notice("npm", "bare", "MIT", ())], self.folder)[:1]
        self.assertEqual(kept.texts, (("LICENSE", "MIT text"),))

    def test_a_blank_file_does_not_hide_that_an_override_is_needed_or_stale(self) -> None:
        # A blank file where the override's package ships none still uses the override.
        found = notices.with_overrides([Notice("npm", "bare", "MIT", (("LICENSE", " "),))], self.folder)
        self.assertEqual(found[0].texts, (("LICENSE", "reviewed text"),))

    def test_every_committed_override_has_its_text(self) -> None:
        for key, (file_name, source) in self.saved.items():
            with self.subTest(key=key):
                self.assertTrue((notices.OVERRIDE_FOLDER / file_name).is_file())
                self.assertTrue(source.strip())


class LicenceFileTest(unittest.TestCase):
    def test_licence_file_names(self) -> None:
        for path in (
            "LICENSE",
            "LICENCE",
            "license",
            "LICENSE.md",
            "LICENSE.txt",
            "LICENSE.html",
            "license-mit",
            "license-cc0",
            "LICENSE-MIT.txt",
            "LICENSE-2.0.txt",
            "LICENSE.BSD",
            "LICENSE.APACHE2",
            "LICENSE.Apache-2.0",
            "LICENSES",
            "COPYING",
            "COPYING.LESSER",
            "NOTICE",
            "NOTICE.txt",
            "COPYRIGHT",
            "Copyright.txt",
            "UNLICENSE",
            "CopyrightNotice.txt",
            "THIRD-PARTY-NOTICES.md",
            "ThirdPartyNotices.txt",
            "vendor/LICENSE",
        ):
            with self.subTest(path=path):
                self.assertTrue(notices.is_licence_file(path))

    def test_every_file_in_a_licence_folder(self) -> None:
        for path in (
            "LICENSES/GPL-3.0.txt",
            "licenses/Apache-2.0",
            "pkg-1.0.dist-info/licenses/AUTHORS.rst",
            "pkg-1.0.dist-info/licenses/vendor/zlib.txt",
            "third_party/notices/README",
            "copying/LGPL.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(notices.is_licence_file(path))

    def test_code_data_and_images_are_not_licence_texts(self) -> None:
        for path in (
            "license.js",
            "lib/licenses.json",
            "notice.ts",
            "license-url.test.ts",
            "license.d.ts",
            "icons/license.svg",
            "icons/copyright-off.svg",
            "licenses/__init__.py",
            "LICENSES/index.json",
            "AUTHORS",
            "README.md",
            "noticeboard.txt",
            "mylicense.txt",
            "licensed/README.md",
        ):
            with self.subTest(path=path):
                self.assertFalse(notices.is_licence_file(path))

    def test_a_licence_like_file_of_an_unknown_kind_stops_the_run(self) -> None:
        for path in ("license.mit", "LICENSES/GPL.unknownext", "NOTICE.docx"):
            with self.subTest(path=path), self.assertRaisesRegex(notices.NoticeError, "classify"):
                notices.is_licence_file(path)


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
