from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from vault_spikes import report as r

PRIVATE_TEXT = "Patient diagnosis: confidential"


def environment() -> r.Environment:
    return r.Environment("Windows-11", "3.12.1", "cpu", 8, 16.0, None, {"numpy": "2.0"})


def make() -> r.Report:
    return r.Report("embedding", "Title", "Q?", "How.", environment())


class ReportRulesTest(unittest.TestCase):
    def test_numbers_and_booleans_are_accepted(self) -> None:
        report = make()
        report.add("model-a.cpu", chunks=10, rate=2.34567891, loads=True)
        self.assertEqual(report.rows[0].metrics, {"chunks": 10, "rate": 2.3457, "loads": True})

    def test_text_values_are_refused(self) -> None:
        for value in (PRIVATE_TEXT, "ok", None, [1], b"x"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                make().add("model", detail=value)  # type: ignore[arg-type]

    def test_non_finite_numbers_are_refused(self) -> None:
        for value in (float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                make().add("model", rate=value)

    def test_names_are_checked_with_fullmatch(self) -> None:
        bad_candidates = [
            "",
            "Model",
            "a b",
            "model\n",
            "model\r",
            "a/b",
            "../x",
            PRIVATE_TEXT,
            "x" * 70,
        ]
        for name in bad_candidates:
            with self.subTest(candidate=name), self.assertRaises(ValueError):
                make().add(name, n=1)
        for name in ("Bad", "a b", "n\n", "", "1abc", PRIVATE_TEXT):
            with self.subTest(metric=name), self.assertRaises(ValueError):
                make().add("model", **{name: 1})
        for spike in ("Embedding", "a b", "x\n", ""):
            with self.subTest(spike=spike), self.assertRaises(ValueError):
                r.Report(spike, "t", "q", "m", environment())

    def test_nothing_reaches_the_output_unless_it_passed_a_check(self) -> None:
        report = make()
        report.add("model-a", chunks=3)
        text = json.dumps(report.to_json()) + report.to_markdown()
        self.assertNotIn(PRIVATE_TEXT, text)


class OutputTest(unittest.TestCase):
    def test_the_markdown_has_the_five_sections_of_a_spike_file(self) -> None:
        report = make()
        report.add("model-a", chunks=3, rate=1.5)
        report.add("model-b", chunks=4)
        markdown = report.to_markdown()
        for section in r.SECTIONS:
            self.assertIn(f"\n## {section}\n", markdown)
        self.assertIn("| model-a | 3 | 1.5 |", markdown)
        self.assertIn("| model-b | 4 |  |", markdown)
        self.assertIn(r.CONCLUSION_PLACEHOLDER, markdown)

    def test_writing_and_reading_back(self) -> None:
        report = make()
        report.add("model-a", chunks=3, fast=False)
        with tempfile.TemporaryDirectory() as folder:
            json_path, md_path = report.write(Path(folder) / "out", stem="embedding-model-a")
            self.assertEqual(json_path.name, "embedding-model-a.json")
            self.assertTrue(md_path.is_file())
            again = r.Report.from_json(json.loads(json_path.read_text(encoding="utf-8")))
        self.assertEqual(again.to_json(), report.to_json())

    def test_reading_a_report_applies_the_checks_again(self) -> None:
        data = make().to_json()
        data["rows"] = [{"candidate": "model", "metrics": {"detail": PRIVATE_TEXT}}]
        with self.assertRaises(TypeError):
            r.Report.from_json(data)

    def test_file_stems_cannot_escape_the_folder(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for stem in ("../x", "a/b", "", "A", "x\n", ".."):
                with self.subTest(stem=stem), self.assertRaises(ValueError):
                    make().write(Path(folder), stem=stem)


class MergeTest(unittest.TestCase):
    def test_rows_are_combined(self) -> None:
        one, two = make(), make()
        one.add("a", n=1)
        two.add("b", n=2)
        self.assertEqual([row.candidate for row in r.merge([one, two]).rows], ["a", "b"])

    def test_different_spikes_are_not_merged(self) -> None:
        other = r.Report("ocr", "T", "Q?", "How.", environment())
        with self.assertRaises(ValueError):
            r.merge([make(), other])
        with self.assertRaises(ValueError):
            r.merge([])


class EnvironmentTest(unittest.TestCase):
    def test_installed_memory_is_reported_where_the_os_says(self) -> None:
        ram = r.total_ram_gib()
        if sys.platform == "win32" or sys.platform.startswith("linux"):
            self.assertIsNotNone(ram)
        self.assertTrue(ram is None or 0.25 < ram < 100_000)

    def test_collects_versions_and_marks_missing_packages(self) -> None:
        env = r.collect_environment(("pyyaml", "no-such-package-xyz"), gpu="GPU 1")
        self.assertEqual(env.packages["no-such-package-xyz"], "not installed")
        self.assertNotEqual(env.packages["pyyaml"], "not installed")
        self.assertEqual(env.gpu, "GPU 1")
        self.assertTrue(env.python)
        self.assertEqual(r.Environment.from_json(env.to_json()), env)


if __name__ == "__main__":
    unittest.main()
