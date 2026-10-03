from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vault_spikes import report as r

PRIVATE_TEXT = "Patient diagnosis: confidential"
KNOWN = {
    "embedding": r.SpikeSchema(
        r.SpikeText("Title", "Q?", "How."),
        re.compile(r"model(?:-[a-z]+)?|a|b|model-cpu|model-cuda"),
        frozenset({"chunks", "fast", "rate", "n", "detail"}),
        frozenset({"numpy", "torch"}),
    )
}


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
            again = r.Report.from_json(json.loads(json_path.read_text(encoding="utf-8")), KNOWN)
        self.assertEqual(again.to_json(), report.to_json())

    def test_reading_a_report_applies_the_checks_again(self) -> None:
        data = make().to_json()
        data["rows"] = [{"candidate": "model", "metrics": {"detail": PRIVATE_TEXT}}]
        with self.assertRaises(TypeError):
            r.Report.from_json(data, KNOWN)

    def test_free_text_in_the_title_question_or_method_is_refused(self) -> None:
        for field in ("title", "question", "method"):
            data = make().to_json()
            data[field] = PRIVATE_TEXT
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "own text"):
                r.Report.from_json(data, KNOWN)

    def test_a_trailing_space_or_line_break_is_not_the_spikes_own_text(self) -> None:
        for field in ("title", "question", "method"):
            for suffix in (" ", "\n", "\r"):
                data = make().to_json()
                data[field] = str(data[field]) + suffix
                with self.subTest(field=field, suffix=suffix), self.assertRaises(ValueError):
                    r.Report.from_json(data, KNOWN)

    def test_a_report_from_an_unknown_spike_is_refused(self) -> None:
        for name in ("ocr", "", PRIVATE_TEXT, None, 1, ["embedding"]):
            data = make().to_json()
            data["spike"] = name
            with self.subTest(spike=name), self.assertRaises(ValueError):
                r.Report.from_json(data, KNOWN)

    def test_the_merged_report_carries_the_spikes_own_text_not_the_files(self) -> None:
        report = r.Report.from_json(make().to_json(), KNOWN)
        self.assertEqual(
            (report.title, report.question, report.method), tuple(KNOWN["embedding"].text)
        )

    def test_a_name_of_the_right_shape_is_still_refused_if_the_spike_cannot_produce_it(
        self,
    ) -> None:
        # Lower-case words pass the shape of a candidate or a metric name, and are free text.
        text = "patient-diagnosis-confidential"
        for kind, rows in (
            ("candidate", [{"candidate": text, "metrics": {"chunks": 1}}]),
            ("metric", [{"candidate": "a", "metrics": {"medical_record_number": 1}}]),
            ("metric", [{"candidate": "a", "metrics": {"chunks": 1, text.replace("-", "_"): 1}}]),
        ):
            data = make().to_json()
            data["rows"] = rows
            with (
                self.subTest(kind=kind, rows=rows),
                self.assertRaisesRegex(ValueError, f"{kind} name this spike cannot"),
            ):
                r.Report.from_json(data, KNOWN)

    def test_the_refusal_never_repeats_the_name(self) -> None:
        data = make().to_json()
        data["rows"] = [{"candidate": PRIVATE_TEXT.lower(), "metrics": {}}]
        with self.assertRaises(ValueError) as caught:
            r.Report.from_json(data, KNOWN)
        self.assertNotIn(PRIVATE_TEXT.lower(), str(caught.exception))

    def test_names_of_the_right_shape_but_the_wrong_kind_are_refused(self) -> None:
        for candidate in (None, 5, ["a"], {"a": 1}):
            data = make().to_json()
            data["rows"] = [{"candidate": candidate, "metrics": {}}]
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                r.Report.from_json(data, KNOWN)

    def test_a_trailing_line_break_does_not_make_a_name_known(self) -> None:
        for candidate in ("a\n", "a ", "a\r", "model-cpu\n"):
            data = make().to_json()
            data["rows"] = [{"candidate": candidate, "metrics": {}}]
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                r.Report.from_json(data, KNOWN)
        data = make().to_json()
        data["rows"] = [{"candidate": "a", "metrics": {"chunks\n": 1}}]
        with self.assertRaises(ValueError):
            r.Report.from_json(data, KNOWN)

    def test_a_package_name_the_spike_cannot_list_is_refused_in_both_places(self) -> None:
        text = "patient-diagnosis-confidential"
        data = make().to_json()
        data["environment"] = {**environment().to_json(), "packages": {text: "1.0"}}
        with self.assertRaisesRegex(ValueError, "package name this spike cannot"):
            r.Report.from_json(data, KNOWN)
        cpu, gpu = make(), make()
        gpu.environment = r.Environment("Windows 11", "3.12.1", "cpu", 8, 16.0, None, {text: "1.0"})
        cpu.add("a", n=1)
        gpu.add("b", n=2)
        with self.assertRaisesRegex(ValueError, "package name this spike cannot"):
            r.Report.from_json(r.merge([cpu, gpu]).to_json(), KNOWN)

    def test_free_text_in_the_environment_is_refused(self) -> None:
        long_text = "a" * 101
        for field, value in (
            ("system", "Patient 'A' > 5\nresult"),
            ("system", long_text),
            ("cpu", PRIVATE_TEXT + "<b>"),
            ("cpu", ["cpu"]),
            ("cpu", {"x": "y"}),
            ("cpu", ""),
            ("cpu", "cpu\n"),
            ("python", "3.12 secret words"),
            ("python", "x"),
            ("gpu", "gpu\n"),
            ("gpu", long_text),
            ("cpu_count", "8"),
            ("cpu_count", True),
            ("cpu_count", -1),
            ("ram_gib", [16.0]),
            ("packages", {"numpy": "see the document"}),
            ("packages", {"patient record": "1.0"}),
            ("packages", {"numpy": "1.0\n"}),
            ("python", "3.12.4\n"),
            ("python", "3.12.4 "),
            ("python", "3.12.confidential"),
            ("python", "3.12.4+confidential"),
        ):
            data = make().to_json()
            env = dict(data["environment"])  # type: ignore[call-overload]
            env[field] = value
            data["environment"] = env
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                r.Report.from_json(data, KNOWN)

    def test_real_machine_descriptions_are_accepted(self) -> None:
        env = r.Environment(
            "Windows-11-10.0.26200-SP0",
            "3.12.15",
            "AMD64 Family 25 Model 97 Stepping 2, AuthenticAMD",
            16,
            63.2,
            "NVIDIA GeForce RTX 3080, 12288 MiB",
            {"sentence-transformers": "5.1.0", "torch": "2.8.0+cu128", "rapidocr": "not installed"},
        )
        self.assertEqual(r.Environment.from_json(env.to_json()), env)

    def test_the_descriptions_taken_from_the_machine_are_known_words_or_unknown(self) -> None:
        for text in ("", "   ", "é" * 300, "\n", "Patient diagnosis confidential", "x" * 101):
            with self.subTest(text=text[:20]):
                self.assertEqual(r.machine_text(text), "unknown")
        for text in ("NVIDIA GeForce RTX 3080", "AMD Ryzen 7 7800X3D 8-Core Processor"):
            with self.subTest(text=text):
                self.assertEqual(r.machine_text(f"  {text}  "), text)

    def test_a_machine_description_is_built_from_known_words_and_model_numbers(self) -> None:
        # Letters, digits and spaces are not enough: a word the vocabulary does not know is free
        # text, whatever it looks like, in any field a merge reads back from a file.
        for field, value in (
            ("cpu", "Patient diagnosis confidential"),
            ("cpu", "AMD Ryzen secret"),
            ("cpu", "Intel Core meeting notes for tuesday"),
            ("gpu", "NVIDIA GeForce RTX confidential"),
            ("gpu", "Dr Smith"),
            ("system", "Windows 11 budget"),
            ("system", "Linux-6.18.44-fc-v64"),
            ("cpu", "?"),
            ("cpu", "AMD\n"),
            ("cpu", "AMD "[:3] + "é"),
        ):
            data = make().to_json()
            env = dict(data["environment"])  # type: ignore[call-overload]
            env[field] = value
            data["environment"] = env
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                r.Report.from_json(data, KNOWN)
        for text in (
            "Windows-11-10.0.26200-SP0",
            "Intel(R) Core(TM) i7-12700K",
            "AMD64 Family 25 Model 97 Stepping 2, AuthenticAMD",
            "NVIDIA GeForce RTX 3080 Laptop GPU",
            "arm64",
        ):
            with self.subTest(text=text):
                self.assertTrue(r.is_machine_text(text))

    def test_every_vocabulary_word_is_a_hardware_or_system_word(self) -> None:
        # The list is a reviewed set of names, so it must stay lower-case and free of sentences.
        for word in r.MACHINE_WORDS:
            with self.subTest(word=word):
                self.assertEqual(word, word.lower())
                self.assertRegex(word, r"[a-z0-9]{1,12}")

    def test_the_system_is_a_name_and_a_release_number_only(self) -> None:
        with (
            mock.patch("platform.system", return_value="Linux"),
            mock.patch("platform.release", return_value="6.18.44-fc-v64"),
        ):
            self.assertEqual(r.system_description(), "Linux 6.18.44")
        with (
            mock.patch("platform.system", return_value="Windows"),
            mock.patch("platform.release", return_value="11"),
        ):
            self.assertEqual(r.system_description(), "Windows 11")
        with (
            mock.patch("platform.system", return_value="MyCompanyOS"),
            mock.patch("platform.release", return_value="x"),
        ):
            self.assertEqual(r.system_description(), "unknown")

    def test_a_cpu_or_gpu_name_the_vocabulary_lacks_is_stored_as_unknown(self) -> None:
        with mock.patch("platform.processor", return_value="Acme Quantum Wibble 9"):
            env = r.collect_environment(gpu="Acme Frobnicator")
        self.assertEqual((env.cpu, env.gpu), ("unknown", "unknown"))
        self.assertEqual(r.Environment.from_json(env.to_json()), env)

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

    def gpu_environment(self) -> r.Environment:
        return r.Environment(
            "Windows-11",
            "3.12.1",
            "cpu",
            8,
            16.0,
            "NVIDIA RTX",
            {"numpy": "2.0", "torch": "2.8+cu128"},
        )

    def test_a_candidate_keeps_the_environment_it_was_measured_in(self) -> None:
        cpu, gpu = make(), make()
        gpu.environment = self.gpu_environment()
        cpu.add("model-cpu", n=1)
        gpu.add("model-cuda", n=2)
        merged = r.merge([cpu, gpu])
        self.assertIsNone(merged.rows[0].environment)  # same as the report's
        self.assertEqual(merged.rows[1].environment, self.gpu_environment())
        # the order of the reports does not change which environment a row is measured in
        reverse = r.merge([gpu, cpu])
        self.assertEqual(reverse.environment, self.gpu_environment())
        self.assertEqual(reverse.rows[1].environment, environment())

    def test_the_markdown_shows_each_environment_once_and_names_the_candidates(self) -> None:
        cpu, gpu = make(), make()
        gpu.environment = self.gpu_environment()
        cpu.add("model-cpu", n=1)
        gpu.add("model-cuda", n=2)
        text = r.merge([cpu, gpu]).to_markdown()
        self.assertIn("Environment of model-cpu:", text)
        self.assertIn("Environment of model-cuda:", text)
        self.assertIn("- GPU: none reported", text)
        self.assertIn("- GPU: NVIDIA RTX", text)
        self.assertIn("torch: 2.8+cu128", text)

    def test_a_report_with_one_environment_reads_as_before(self) -> None:
        one, two = make(), make()
        one.add("a", n=1)
        two.add("b", n=2)
        merged = r.merge([one, two])
        self.assertTrue(all(row.environment is None for row in merged.rows))
        self.assertNotIn("Environment of", merged.to_markdown())

    def test_a_row_environment_survives_the_json_round_trip_and_is_checked(self) -> None:
        cpu, gpu = make(), make()
        gpu.environment = self.gpu_environment()
        cpu.add("model-cpu", n=1)
        gpu.add("model-cuda", n=2)
        merged = r.merge([cpu, gpu])
        again = r.Report.from_json(merged.to_json(), KNOWN)
        self.assertEqual([row.environment for row in again.rows], [None, self.gpu_environment()])
        data = merged.to_json()
        data["rows"][1]["environment"]["cpu"] = PRIVATE_TEXT + "<b>"  # type: ignore[index]
        with self.assertRaises(ValueError):
            r.Report.from_json(data, KNOWN)

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

    def test_adding_a_package_goes_through_the_same_check_as_any_other(self) -> None:
        env = make().environment
        added = r.with_package(env, "tesseract", "5.5.0")
        self.assertEqual(added.packages["tesseract"], "5.5.0")
        self.assertNotIn("tesseract", env.packages)  # the original is untouched
        self.assertEqual(r.Environment.from_json(added.to_json()), added)
        for version in ("tesseract 5.5.0", "5.5.0\n", "see the document", "", "5.5.0 "):
            with self.subTest(version=version), self.assertRaises(ValueError):
                r.with_package(env, "tesseract", version)
        with self.assertRaises(ValueError):
            r.with_package(env, "patient record", "1.0")

    def test_a_package_value_is_a_version_or_a_sentinel_never_a_word(self) -> None:
        # Words that pass a loose shape check (letters, digits, dots) are still free text.
        for word in (
            "patient-diagnosis-confidential",
            "confidential",
            "1.0.confidential",
            "2.8.0+confidential",
            "2.8.0+cu128.patient",
            "1.0.0-patient",
            "v1.0",
            "1.0 ",
            "1.0\n",
            "1.0\r",
            "1.2.3.4.5.6",
            "Unknown",
            "not  installed",
            "not installed\n",
            "",
        ):
            with self.subTest(word=word):
                self.assertIsNone(r.PACKAGE_VERSION.fullmatch(word))
                with self.assertRaises(ValueError):
                    r.with_package(make().environment, "torch", word)
                data = make().to_json()
                env = dict(data["environment"])  # type: ignore[call-overload]
                env["packages"] = {"torch": word}
                data["environment"] = env
                with self.assertRaises(ValueError):
                    r.Report.from_json(data, KNOWN)

    def test_real_versions_and_the_sentinels_are_accepted(self) -> None:
        for version in (
            "2.8.0+cu128",
            "2.8.0+cpu",
            "2.8.0+rocm6.4",
            "5.3.3.20231005",
            "1.0.0rc1",
            "1.0.0a2",
            "0.5.0.dev0",
            "1.0.0.post1",
            "1!2.0",
            "2024.8.30",
            "4.30.0",
            "0.1.6",
            "not installed",
            "unknown",
        ):
            with self.subTest(version=version):
                added = r.with_package(make().environment, "torch", version)
                self.assertEqual(r.Environment.from_json(added.to_json()), added)

    def test_a_version_found_on_the_machine_that_does_not_fit_is_unknown(self) -> None:
        self.assertEqual(r.package_version("2.8.0+cu128"), "2.8.0+cu128")
        for odd in ("2.8.0+git.abc1234", "custom-build", "1.0 beta", "1.0\n"):
            with self.subTest(odd=odd):
                self.assertEqual(r.package_version(odd), "unknown")
        with mock.patch("importlib.metadata.version", return_value="2.0+my-private-build"):
            env = r.collect_environment(("pyyaml",))
        self.assertEqual(env.packages["pyyaml"], "unknown")

    def test_collects_versions_and_marks_missing_packages(self) -> None:
        env = r.collect_environment(("pyyaml", "no-such-package-xyz"), gpu="GPU 1")
        self.assertEqual(env.packages["no-such-package-xyz"], "not installed")
        self.assertNotEqual(env.packages["pyyaml"], "not installed")
        self.assertEqual(env.gpu, "GPU 1")
        self.assertTrue(env.python)
        self.assertEqual(r.Environment.from_json(env.to_json()), env)


if __name__ == "__main__":
    unittest.main()
