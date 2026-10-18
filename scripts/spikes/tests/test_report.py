from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest import mock

from vault_spikes import report as r
from vault_spikes.errors import OwnValueError

PRIVATE_TEXT = "Patient diagnosis: confidential"
TOY_METRICS = {
    "chunks": r.Kind.COUNT,
    "fast": r.Kind.AMOUNT,
    "rate": r.Kind.AMOUNT,
    "n": r.Kind.AMOUNT,
    "detail": r.Kind.AMOUNT,
    "share": r.Kind.RATIO,
    "ok": r.Kind.FLAG,
}
KNOWN = {
    "embedding": r.SpikeSchema(
        r.SpikeText("Title", "Q?", "How."),
        (
            # Rows of the other tests: nothing is required, so a row may carry any one metric.
            r.RowShape(
                re.compile(r"model(?:-[a-z]+)?|a|b|model-cpu|model-cuda"),
                frozenset(),
                frozenset(TOY_METRICS),
            ),
            # A row with context it cannot lose (chunks, ok), and one figure it may lack (fast).
            r.RowShape(re.compile(r"strict"), frozenset({"chunks", "ok"}), frozenset({"fast"})),
            r.RowShape(re.compile(r"plain"), frozenset({"chunks"})),
        ),
        TOY_METRICS,
        frozenset({"numpy", "torch"}),
        frozenset({"numpy"}),
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
        report.add("model-a", chunks=3, ok=False)
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

    def test_a_gpu_found_after_the_run_is_stored_like_one_found_before_it(self) -> None:
        # The CLI learns the card after the measurement, so a name with a word the list lacks must
        # become ``unknown``, not an error that throws the measurement away.
        env = make().environment
        for name, expected in (
            ("NVIDIA GeForce RTX 3080", "NVIDIA GeForce RTX 3080"),
            ("NVIDIA RTX 4000 Ada Generation", "unknown"),
            ("Patient diagnosis confidential", "unknown"),
            ("RTX 3080\n", "RTX 3080"),  # the line break is stripped, not kept
        ):
            with self.subTest(name=name):
                added = r.with_gpu(env, name)
                self.assertEqual(added.gpu, expected)
                self.assertEqual(r.Environment.from_json(added.to_json()), added)
        self.assertIsNone(r.with_gpu(env, None).gpu)
        self.assertEqual(env.gpu, make().environment.gpu)  # the original is untouched

    def test_a_cpu_or_gpu_name_the_vocabulary_lacks_is_stored_as_unknown(self) -> None:
        with mock.patch("platform.processor", return_value="Acme Quantum Wibble 9"):
            env = r.collect_environment(gpu="Acme Frobnicator")
        self.assertEqual((env.cpu, env.gpu), ("unknown", "unknown"))
        self.assertEqual(r.Environment.from_json(env.to_json()), env)

    def test_a_report_is_never_written_over_an_existing_one(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            make().write(root, stem="a")
            before = (root / "a.json").read_bytes()
            (root / "b.md").write_text("only the markdown exists", encoding="utf-8")
            stems = ["a", "b"]
            try:
                (root / "c.json").symlink_to(root / "nowhere")  # a dangling link is not "free"
                stems.append("c")
            except OSError:  # Windows without the privilege to make links
                pass
            for stem in stems:
                with self.subTest(stem=stem), self.assertRaises(OwnValueError) as caught:
                    make().write(root, stem=stem)
                self.assertNotIn(folder, str(caught.exception))
            self.assertEqual((root / "a.json").read_bytes(), before)
            self.assertFalse((root / "nowhere").exists())
            self.assertEqual(
                (root / "b.md").read_text(encoding="utf-8"), "only the markdown exists"
            )
            # A derived report (a merge) may be replaced, but its name is still checked.
            report = make()
            report.add("model-a", chunks=7)
            json_path, _ = report.write(root, stem="a", overwrite=True)
            self.assertIn('"chunks": 7', json_path.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                report.write(root, stem="../x", overwrite=True)

    def test_a_report_or_link_that_appears_after_the_check_is_kept(self) -> None:
        # ``require_free`` runs before the measurement, which can last an hour; a file made in
        # the folder after it must be neither replaced nor followed when the report is written.
        for suffix in (".json", ".md"):
            for kind in ("file", "link"):
                with self.subTest(suffix=suffix, kind=kind):
                    self.check_appeared(suffix, kind)

    def check_appeared(self, suffix: str, kind: str) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            outside = root / "outside.txt"
            outside.write_text("keep", encoding="utf-8")
            appeared = root / f"a{suffix}"

            def meanwhile(directory: Path, stem: str) -> None:
                if kind == "file":
                    appeared.write_text("someone else's", encoding="utf-8")
                    return
                try:
                    appeared.symlink_to(outside)
                except OSError:  # Windows without the privilege to make links
                    self.skipTest("cannot create symbolic links here")

            with (
                mock.patch.object(r, "require_free", side_effect=meanwhile),
                self.assertRaises(OwnValueError) as caught,
            ):
                make().write(root, stem="a")
            self.assertNotIn(folder, str(caught.exception))
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            if kind == "file":
                self.assertEqual(appeared.read_text(encoding="utf-8"), "someone else's")
            self.assertEqual({p.name for p in root.iterdir()}, {"outside.txt", f"a{suffix}"})

    def test_a_first_report_that_another_process_replaced_is_not_removed(self) -> None:
        # The .md name is refused, so the .json this call made is cleaned up; but another process
        # may have removed that .json meanwhile and written its own, which must stay. Windows
        # refuses to remove the .json while this call holds it open, so there the other process
        # only takes the .md name, and the cleanup removes this call's own .json.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            real_open = Path.open

            def take(name: str) -> None:
                with open(os.path.join(folder, name), "w", encoding="utf-8") as other:
                    other.write("someone else's")

            def swapping_open(path: Path, mode: str = "r", **kwargs: Any) -> Any:
                if path.name == "a.md":
                    if r.WINDOWS:
                        with self.assertRaises(PermissionError):
                            os.unlink(os.path.join(folder, "a.json"))
                    else:
                        os.unlink(os.path.join(folder, "a.json"))
                        take("a.json")
                    take("a.md")
                return real_open(path, mode, **kwargs)

            with (
                mock.patch.object(Path, "open", new=swapping_open),
                self.assertRaises(OwnValueError),
            ):
                make().write(root, stem="a")
            self.assertEqual((root / "a.md").read_text(encoding="utf-8"), "someone else's")
            if r.WINDOWS:
                self.assertFalse((root / "a.json").exists())
            else:
                self.assertEqual((root / "a.json").read_text(encoding="utf-8"), "someone else's")

    def test_remove_own_removes_only_the_file_open_on_the_handle(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            mine = root / "mine.txt"
            other = root / "other.txt"

            def create(path: Path) -> int:
                return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)

            def assert_closed(handle: int) -> None:
                with self.assertRaises(OSError):
                    os.fstat(handle)

            handle = create(mine)
            r.remove_own(mine, handle, lambda: os.close(handle))
            self.assertFalse(os.path.lexists(mine))
            assert_closed(handle)

            # The name holds another file than the one open on the handle. Elsewhere than
            # Windows the open file is moved away and the name taken; Windows cannot move an open
            # file, so there the handle is opened on a different file from the start.
            if r.WINDOWS:
                handle = create(other)
            else:
                handle = create(mine)
                mine.replace(other)
            mine.write_text("someone else's", encoding="utf-8")
            r.remove_own(mine, handle, lambda: os.close(handle))
            self.assertEqual(mine.read_text(encoding="utf-8"), "someone else's")
            self.assertTrue(other.exists())
            assert_closed(handle)
            mine.unlink()

            handle = create(root / "linked.txt") if r.WINDOWS else create(mine)
            try:
                if not r.WINDOWS:
                    mine.replace(root / "linked.txt")
                mine.symlink_to(other)  # a link to a file is not the file
            except OSError:  # Windows without the privilege to make links
                os.close(handle)
            else:
                r.remove_own(mine, handle, lambda: os.close(handle))
                self.assertTrue(mine.is_symlink())
                assert_closed(handle)

    @unittest.skipIf(r.WINDOWS, "Windows does not let another process remove a file open here")
    def test_remove_own_after_another_process_removed_the_open_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            mine = Path(folder) / "mine.txt"
            handle = os.open(mine, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            mine.unlink()  # removed by another process: nothing to remove, still closed
            r.remove_own(mine, handle, lambda: os.close(handle))
            with self.assertRaises(OSError):
                os.fstat(handle)

            handle = os.open(mine, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            mine.unlink()  # removed and made again at once: the same number on Linux
            mine.write_text("someone else's", encoding="utf-8")
            r.remove_own(mine, handle, lambda: os.close(handle))
            self.assertEqual(mine.read_text(encoding="utf-8"), "someone else's")

    @unittest.skipIf(r.WINDOWS, "Windows closes first, as it cannot remove an open file")
    def test_remove_own_compares_before_the_file_number_can_be_reused(self) -> None:
        # Once the descriptor is closed, a removed file's number can go to the next new file (at
        # once on Linux), so a comparison made after closing would take that file for its own.
        with tempfile.TemporaryDirectory() as folder:
            mine = Path(folder) / "mine.txt"
            handle = os.open(mine, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            real_close = os.close

            def close_then_replaced(fd: int) -> None:
                real_close(fd)
                if fd == handle:
                    with contextlib.suppress(FileNotFoundError):
                        mine.unlink()
                    mine.write_text("someone else's", encoding="utf-8")

            with mock.patch.object(os, "close", side_effect=close_then_replaced):
                r.remove_own(mine, handle, lambda: os.close(handle))
            self.assertEqual(mine.read_text(encoding="utf-8"), "someone else's")

    def test_file_stems_cannot_escape_the_folder(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for stem in ("../x", "a/b", "", "A", "x\n", ".."):
                with self.subTest(stem=stem), self.assertRaises(ValueError):
                    make().write(Path(folder), stem=stem)


class MetricValueTest(unittest.TestCase):
    """A report read back from a file is refused when a value is outside what its metric can be."""

    def report_with(self, **metrics: object) -> dict[str, object]:
        report = make()
        report.add("a", n=1)
        data = report.to_json()
        rows = data["rows"]
        assert isinstance(rows, list)
        rows[0]["metrics"] = metrics
        return data

    def test_values_inside_each_kind_are_accepted(self) -> None:
        data = self.report_with(chunks=0, share=1.0, ok=False, fast=0.5, rate=10**12)
        self.assertEqual(r.Report.from_json(data, KNOWN).rows[0].metrics["share"], 1.0)
        for share in (0, 0.0, 1, 0.25):
            r.Report.from_json(self.report_with(share=share), KNOWN)
        r.Report.from_json(self.report_with(ok=True), KNOWN)

    def test_values_outside_a_kind_are_refused_and_not_quoted(self) -> None:
        for metrics in (
            {"share": -5},
            {"share": 1.5},
            {"share": True},
            {"chunks": 1.5},
            {"chunks": -1},
            {"chunks": True},
            {"chunks": 10**13},
            {"fast": -0.1},
            {"fast": True},
            {"fast": 10**13},
            {"ok": 1},
            {"ok": 0.0},
        ):
            with self.subTest(metrics=metrics):
                data = self.report_with(**metrics)
                with self.assertRaises(OwnValueError) as caught:
                    r.Report.from_json(data, KNOWN)
                (name,) = metrics
                self.assertIn(name, str(caught.exception))
                self.assertNotIn(str(next(iter(metrics.values()))), str(caught.exception))

    def test_text_in_place_of_a_number_is_still_refused(self) -> None:
        for value in ("12", None, [1], {"a": 1}):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                r.Report.from_json(self.report_with(chunks=value), KNOWN)

    def test_in_domain_covers_every_kind(self) -> None:
        self.assertEqual(set(r.Kind), {r.Kind.COUNT, r.Kind.RATIO, r.Kind.AMOUNT, r.Kind.FLAG})
        self.assertFalse(r.in_domain(r.Kind.COUNT, 2.0))  # a count is a whole number
        self.assertTrue(r.in_domain(r.Kind.AMOUNT, 2))


class RoundingTest(unittest.TestCase):
    """The rules that compare a figure with the ones it was computed from allow exactly the
    rounding that ``Report.add`` applies, so a report the script wrote always passes and an edited
    figure does not."""

    def test_a_stored_value_may_lie_half_a_last_decimal_from_the_computed_one(self) -> None:
        self.assertEqual(r.ROUNDING, 0.00005)
        computed = 2 / 3
        self.assertTrue(r.rounded_within(round(computed, r.DECIMALS), computed, computed))
        self.assertTrue(r.rounded_within(0.6667, 0.66672, 0.7))
        self.assertFalse(r.rounded_within(0.6667, 0.66676, 0.7))  # more than 0.00005 below
        self.assertFalse(r.rounded_within(0.7001, 0.6, 0.70004))  # more than 0.00005 above
        # A large figure keeps its last decimal: the slack does not grow with the value.
        self.assertFalse(r.rounded_within(1000.0002, 1000.0, 1000.0))

    def test_a_whole_share_is_a_whole_number_of_parts(self) -> None:
        for share, count in (
            (0.0, 3),
            (0.3333, 3),
            (0.6667, 3),
            (1.0, 3),
            (0.5, 2),
            (0.0001, 7000),
        ):
            with self.subTest(share=share, count=count):
                self.assertTrue(r.whole_share(share, count))
        for share, count in ((0.5, 3), (0.3, 2), (0.333, 3), (0.5, 0)):
            with self.subTest(share=share, count=count):
                self.assertFalse(r.whole_share(share, count))

    def test_a_right_share_leaves_room_for_its_wrong_answers(self) -> None:
        # (right, wrong) -> right / (right + wrong), rounded as a report stores it.
        for right, wrong in ((0, 1), (1, 1), (2, 1), (3, 1), (3, 2), (0, 4), (999, 1), (5, 0)):
            share = round(right / (right + wrong), r.DECIMALS)
            with self.subTest(right=right, wrong=wrong):
                self.assertTrue(r.right_share(share, wrong))
        self.assertTrue(r.right_share(1.0, 1))  # 1 wrong among 20,000 or more rounds to 1.0
        for share, wrong in ((0.6, 1), (0.85, 1), (0.7, 2), (0.9999, 0), (0.5, 0)):
            with self.subTest(share=share, wrong=wrong):
                self.assertFalse(r.right_share(share, wrong))


class PackageBaselineTest(unittest.TestCase):
    """A report must list the packages every report of its spike lists (the versions that make a
    measurement reproducible), and may add only the extras the spike allows."""

    def read(self, packages: dict[str, str], row: dict[str, str] | None = None) -> r.Report:
        data = make().to_json()
        env = cast("dict[str, object]", data["environment"])
        env["packages"] = packages
        if row is not None:
            data["rows"] = [
                {
                    "candidate": "model",
                    "metrics": {},
                    "environment": {**env, "packages": row},
                }
            ]
        return r.Report.from_json(data, KNOWN)

    def test_the_baseline_alone_and_with_an_allowed_extra_is_accepted(self) -> None:
        self.read({"numpy": "2.0"})
        self.read({"numpy": "2.0", "torch": "2.5"})

    def test_a_report_without_its_baseline_packages_is_refused(self) -> None:
        for packages in ({}, {"torch": "2.5"}):
            with self.subTest(packages=packages), self.assertRaisesRegex(OwnValueError, "lacks"):
                self.read(packages)

    def test_a_row_environment_needs_the_baseline_too(self) -> None:
        self.read({"numpy": "2.0"}, {"numpy": "2.1"})
        with self.assertRaisesRegex(OwnValueError, "lacks"):
            self.read({"numpy": "2.0"}, {})

    def test_the_baseline_is_a_subset_of_what_may_be_listed(self) -> None:
        schema = KNOWN["embedding"]
        self.assertLessEqual(schema.required_packages, schema.packages)


class RowShapeTest(unittest.TestCase):
    """A report read back must give each row all the metrics its candidate always has, and only
    metrics that candidate can emit."""

    def read(self, candidate: str, metrics: Mapping[str, object]) -> r.Report:
        data = make().to_json()
        data["rows"] = [{"candidate": candidate, "metrics": metrics}]
        return r.Report.from_json(data, KNOWN)

    def test_a_complete_row_is_accepted_with_or_without_its_optional_figure(self) -> None:
        self.read("strict", {"chunks": 3, "ok": True})
        self.read("strict", {"chunks": 3, "ok": True, "fast": 0.5})

    def test_a_row_without_its_context_is_refused(self) -> None:
        for metrics in ({}, {"chunks": 3}, {"ok": True}, {"fast": 0.5}):
            with (
                self.subTest(metrics=metrics),
                self.assertRaisesRegex(OwnValueError, "without metrics"),
            ):
                self.read("strict", metrics)

    def test_an_empty_row_is_refused_where_a_metric_is_required(self) -> None:
        with self.assertRaises(OwnValueError):
            self.read("plain", {})

    def test_a_metric_the_candidate_cannot_emit_is_refused_though_the_spike_has_it(self) -> None:
        # "rate" is a metric of the spike (the toy "model" rows carry it), not of these rows.
        for candidate, metrics in (
            ("strict", {"chunks": 3, "ok": True, "rate": 1.0}),
            ("plain", {"chunks": 3, "fast": 1.0}),
        ):
            with (
                self.subTest(candidate=candidate),
                self.assertRaisesRegex(OwnValueError, "metric name"),
            ):
                self.read(candidate, metrics)

    def test_the_error_names_the_kind_of_fault_and_not_the_candidate(self) -> None:
        with self.assertRaises(OwnValueError) as caught:
            self.read("strict", {})
        self.assertNotIn("strict", str(caught.exception))

    def test_the_first_row_that_fits_the_name_decides(self) -> None:
        self.assertEqual(KNOWN["embedding"].shape_of("plain"), KNOWN["embedding"].rows[2])
        self.assertIsNone(KNOWN["embedding"].shape_of("plain\n"))
        self.assertIsNone(KNOWN["embedding"].shape_of(""))


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

    def test_rows_of_one_candidate_on_both_data_sets_are_told_apart(self) -> None:
        # The public and the private run of one candidate are two rows with one name; when the
        # second ran in another environment (a model revision that moved, say), the Markdown must
        # say which row it belongs to.
        public, private = make(), make()
        private.environment = self.gpu_environment()
        public.add("model-cpu", public_dataset=True, n=1)
        private.add("model-cpu", public_dataset=False, n=2)
        text = r.merge([public, private]).to_markdown()
        self.assertIn("Environment of model-cpu (public data):", text)
        self.assertIn("Environment of model-cpu (private data):", text)
        self.assertIn("| model-cpu (public data) |", text)
        self.assertIn("| model-cpu (private data) |", text)

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


REVISION = "0123456789abcdef0123456789abcdef01234567"


class ModelRevisionTest(unittest.TestCase):
    """The commit of the model files is part of the evidence: a model's default branch moves."""

    schema: ClassVar = {"embedding": KNOWN["embedding"]._replace(models=True)}

    def with_revision(self, revision: object) -> dict[str, object]:
        data = make().to_json()
        cast("dict[str, object]", data["environment"])["model_revision"] = revision
        return data

    def test_a_revision_is_40_lower_case_hexadecimal_digits(self) -> None:
        env = r.with_model_revision(environment(), REVISION)
        self.assertEqual(env.model_revision, REVISION)
        self.assertEqual(r.Environment.from_json(env.to_json()), env)
        for bad in ("main", REVISION.upper(), REVISION[:-1], REVISION + "0", REVISION + "\n", ""):
            with self.subTest(bad=bad), self.assertRaises(OwnValueError):
                r.Environment.from_json(self.with_revision(bad)["environment"])  # type: ignore[arg-type]
        for not_text in (12, True, ["a"]):
            with self.subTest(not_text=not_text), self.assertRaises(OwnValueError):
                r.Environment.from_json(self.with_revision(not_text)["environment"])  # type: ignore[arg-type]

    def test_a_revision_found_after_the_run_is_stored_as_none_if_it_is_not_one(self) -> None:
        for bad in (None, "main", "x" * 40, REVISION + "\n"):
            with self.subTest(bad=bad):
                self.assertIsNone(r.with_model_revision(environment(), bad).model_revision)

    def test_an_old_report_without_the_field_reads_as_none(self) -> None:
        data = self.with_revision(None)
        del cast("dict[str, object]", data["environment"])["model_revision"]
        self.assertIsNone(r.Report.from_json(data, self.schema).environment.model_revision)

    def test_only_a_spike_that_loads_a_model_may_name_a_revision(self) -> None:
        r.Report.from_json(self.with_revision(REVISION), self.schema)
        with self.assertRaisesRegex(OwnValueError, "model revision"):
            r.Report.from_json(self.with_revision(REVISION), KNOWN)

    def test_the_markdown_names_the_revision_or_says_it_is_unknown(self) -> None:
        known = make()
        known.environment = r.with_model_revision(known.environment, REVISION)
        self.assertIn(f"- Model revision: {REVISION}", known.to_markdown())
        self.assertIn("- Model revision: unknown", make().to_markdown())

    def test_a_row_made_with_other_files_keeps_its_own_environment_in_a_merge(self) -> None:
        first, second = make(), make()
        first.environment = r.with_model_revision(first.environment, REVISION)
        first.add("a", chunks=1)
        second.add("b", chunks=1)
        text = r.merge([first, second]).to_markdown()
        self.assertIn(REVISION, text)
        self.assertIn("Model revision: unknown", text)
