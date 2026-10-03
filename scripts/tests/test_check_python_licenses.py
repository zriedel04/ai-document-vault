"""Tests for scripts/check_python_licenses.py (standard library only)."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from importlib.metadata import distributions
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import check_python_licenses as licences  # noqa: E402


def package(
    name: str = "demo",
    expression: str | None = None,
    classifiers: tuple[str, ...] = (),
    field: str | None = None,
) -> licences.Package:
    return licences.Package(name, "1.0", expression, classifiers, field)


class ExpressionTest(unittest.TestCase):
    def test_allowed_licences(self) -> None:
        for expression in (
            "MIT",
            "mit",
            "Apache-2.0",
            "BSD-3-Clause",
            "Apache-2.0 OR BSD-2-Clause",
            "MIT AND BSD-3-Clause",
            "(MIT OR GPL-3.0-only) AND Apache-2.0",
            "MIT OR GPL-3.0-only",
            "Zlib",
            "0BSD",
            "Apache-2.0 WITH LLVM-exception",
            "apache-2.0 with llvm-exception",
            "(Apache-2.0 WITH LLVM-exception) AND MIT",
        ):
            with self.subTest(expression=expression):
                self.assertTrue(licences.expression_allowed(expression))

    def test_disallowed_licences(self) -> None:
        for expression in (
            "GPL-3.0-only",
            "GPL-2.0-or-later",
            "AGPL-3.0-only",
            "LGPL-3.0-or-later",
            "MPL-2.0",
            "SSPL-1.0",
            "CC-BY-NC-4.0",
            "LicenseRef-Proprietary",
            "MIT AND GPL-3.0-only",
            "GPL-3.0-only WITH Classpath-exception-2.0",
            "GPL-3.0-only WITH LLVM-exception",
            # An exception is only accepted if it is on the list, whatever the base licence is.
            "MIT WITH GPL-3.0-only",
            "MIT WITH LicenseRef-Proprietary",
            "MIT WITH nonsense",
            "MIT WITH Classpath-exception-2.0",
            "MIT OR Apache-2.0 WITH nonsense",
            "UNKNOWN",
            "Public Domain",
        ):
            with self.subTest(expression=expression):
                self.assertFalse(licences.expression_allowed(expression))

    def test_malformed_expressions_are_rejected(self) -> None:
        for expression in (
            "",
            "MIT OR",
            "MIT AND",
            "(MIT",
            "MIT)",
            "OR MIT",
            "MIT MIT",
            "()",
            "MIT WITH",
            "MIT WITH AND MIT",
            "MIT WITH (",
            "MIT WITH LLVM-exception WITH LLVM-exception",
        ):
            with self.subTest(expression=expression):
                self.assertFalse(licences.expression_allowed(expression))


class PackageTest(unittest.TestCase):
    def test_expression_decides_first(self) -> None:
        self.assertTrue(
            licences.package_allowed(package(expression="MIT", classifiers=("GNU GPL",)))
        )
        viral = package(expression="GPL-3.0-only", classifiers=("MIT License",))
        self.assertFalse(licences.package_allowed(viral))

    def test_classifiers_all_must_be_allowed(self) -> None:
        self.assertTrue(licences.package_allowed(package(classifiers=("MIT License",))))
        self.assertTrue(licences.package_allowed(package(classifiers=("BSD License",))))
        both = package(classifiers=("Apache Software License", "MIT License"))
        self.assertTrue(licences.package_allowed(both))
        self.assertFalse(
            licences.package_allowed(
                package(classifiers=("MIT License", "GNU General Public License v3 (GPLv3)"))
            )
        )
        self.assertFalse(licences.package_allowed(package(classifiers=("Public Domain",))))

    def test_short_license_field_is_read(self) -> None:
        self.assertTrue(licences.package_allowed(package(field="MIT")))
        self.assertTrue(licences.package_allowed(package(field="BSD-3-Clause")))
        self.assertFalse(licences.package_allowed(package(field="GPLv3")))
        self.assertFalse(licences.package_allowed(package(field="UNKNOWN")))

    def test_licence_text_in_the_field_is_not_trusted(self) -> None:
        text = "MIT License\n\nPermission is hereby granted, free of charge, to any person..."
        self.assertFalse(licences.package_allowed(package(field=text)))
        self.assertFalse(licences.package_allowed(package(field="MIT " + "x" * 200)))

    def test_missing_licence_fails(self) -> None:
        self.assertFalse(licences.package_allowed(package()))


class ViolationsTest(unittest.TestCase):
    MPL = package("pathspec", classifiers=("Mozilla Public License 2.0 (MPL 2.0)",))

    def test_reports_each_bad_package(self) -> None:
        problems = licences.violations(
            [package("good", "MIT"), package("bad", "GPL-3.0-only"), package("none")]
        )
        self.assertEqual(len(problems), 2)
        self.assertTrue(problems[0].startswith("bad 1.0: GPL-3.0-only"))
        self.assertIn("no licence found", problems[1])

    def test_reviewed_exception_applies_only_outside_strict_mode(self) -> None:
        self.assertEqual(licences.violations([self.MPL]), [])
        self.assertEqual(len(licences.violations([self.MPL], strict=True)), 1)

    def test_reviewed_exception_stops_when_the_licence_changes(self) -> None:
        changed = package("pathspec", classifiers=("GNU General Public License v3 (GPLv3)",))
        self.assertEqual(len(licences.violations([changed])), 1)

    def test_reviewed_exception_needs_the_winning_metadata_source_to_say_so(self) -> None:
        mpl = "Mozilla Public License 2.0 (MPL 2.0)"
        gpl = "GNU General Public License v3 (GPLv3)"
        # The expression wins over the classifiers, so an old MPL classifier cannot rescue GPL.
        gpl_expression = package("pathspec", "GPL-3.0-only", classifiers=(mpl,))
        self.assertEqual(len(licences.violations([gpl_expression])), 1)
        # The same for the free-text field, behind both of the others.
        gpl_classifier = package("pathspec", classifiers=(gpl,), field=mpl)
        self.assertEqual(len(licences.violations([gpl_classifier])), 1)
        # An extra classifier next to the reviewed one ends the exception.
        extra = package("pathspec", classifiers=(mpl, gpl))
        self.assertEqual(len(licences.violations([extra])), 1)
        # An allowed expression with the reviewed classifier is simply allowed.
        self.assertEqual(licences.violations([package("pathspec", "MIT", classifiers=(mpl,))]), [])
        # The reviewed licence in the winning source is still accepted (outside strict mode).
        self.assertEqual(licences.violations([package("pathspec", classifiers=(mpl,))]), [])
        self.assertEqual(licences.violations([package("pathspec", field=mpl)]), [])
        self.assertEqual(len(licences.violations([package("pathspec", field=mpl)], strict=True)), 1)

    def test_licence_source_follows_the_precedence_of_the_allowed_check(self) -> None:
        self.assertEqual(
            licences.licence_source(package(expression="MIT", classifiers=("x",), field="y")),
            ("MIT",),
        )
        self.assertEqual(licences.licence_source(package(classifiers=("x",), field="y")), ("x",))
        self.assertEqual(licences.licence_source(package(field="y")), ("y",))
        self.assertEqual(licences.licence_source(package(field="y\nz")), ())
        self.assertEqual(licences.licence_source(package()), ())

    def test_pillow_exception_is_for_mit_cmu_only_and_never_strict(self) -> None:
        mit_cmu = package("Pillow", "MIT-CMU")
        self.assertEqual(licences.violations([mit_cmu]), [])
        self.assertEqual(len(licences.violations([mit_cmu], strict=True)), 1)
        self.assertEqual(len(licences.violations([package("Pillow", "GPL-3.0-only")])), 1)
        self.assertEqual(len(licences.violations([package("not-pillow", "MIT-CMU")])), 1)

    def test_exception_is_per_package(self) -> None:
        other = package("something-else", classifiers=("Mozilla Public License 2.0 (MPL 2.0)",))
        self.assertEqual(len(licences.violations([other])), 1)

    def test_name_normalisation(self) -> None:
        self.assertEqual(licences.normalise("Path_Spec.X"), "path-spec-x")
        viral = package("My_Pkg", "GPL-3.0-only")
        self.assertEqual(licences.violations([viral], ignore=["my-pkg"]), [])


class EnvironmentTest(unittest.TestCase):
    """The script reads real metadata: build tiny fake installations and run it."""

    def install(self, root: Path, name: str, metadata: str) -> None:
        info = root / f"{name}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.4\nName: {name}\nVersion: 1.0\n{metadata}", encoding="utf-8"
        )

    def run_main(self, root: Path, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        original = sys.path[:]
        sys.path[:] = [str(root)]
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = licences.main(list(args))
        finally:
            sys.path[:] = original
        return code, err.getvalue()

    def test_reads_metadata_and_fails_the_build_on_a_disallowed_licence(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.install(root, "fine", "License-Expression: MIT\n")
            self.install(root, "viral", "License-Expression: AGPL-3.0-only\n")
            names = {d.metadata["Name"] for d in distributions(path=[str(root)])}
            self.assertEqual(names, {"fine", "viral"})
            code, err = self.run_main(root)
            self.assertEqual(code, 1)
            self.assertIn("viral 1.0: AGPL-3.0-only", err)
            self.assertNotIn("fine", err)

    def test_passes_when_every_package_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.install(root, "fine", "License-Expression: MIT\n")
            self.install(root, "old", "Classifier: License :: OSI Approved :: BSD License\n")
            self.assertEqual(self.run_main(root)[0], 0)

    def test_ignore_skips_the_project_itself(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.install(root, "ours", "")
            self.assertEqual(self.run_main(root)[0], 1)
            self.assertEqual(self.run_main(root, "--ignore", "ours")[0], 0)

    def test_a_gpl_expression_is_not_rescued_by_a_reviewed_classifier(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.install(
                root,
                "pathspec",
                "License-Expression: GPL-3.0-only\n"
                "Classifier: License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)\n",
            )
            self.assertEqual(self.run_main(root)[0], 1)

    def test_strict_mode_rejects_reviewed_exceptions(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            self.install(
                root,
                "pathspec",
                "Classifier: License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)\n",
            )
            self.assertEqual(self.run_main(root)[0], 0)
            self.assertEqual(self.run_main(root, "--strict")[0], 1)


if __name__ == "__main__":
    unittest.main()
