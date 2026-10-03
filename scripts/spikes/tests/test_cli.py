from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest import mock

from vault_spikes import __main__ as cli
from vault_spikes import adapters
from vault_spikes import spike_pdf as pdf
from vault_spikes.report import Environment, Kind, Report

KNOWN = cli.KNOWN


def complete_row(spike: str, candidate: str) -> dict[str, float]:
    """The metrics a row of this candidate must carry, each with a value its kind allows and
    all of them consistent: nothing was tried or scored (zero counts), except for the sqlite-vec
    rows, whose counts say that something was measured."""
    shape = KNOWN[spike].shape_of(candidate)
    assert shape is not None
    values = {
        Kind.COUNT: 1 if spike == "vec" else 0,
        Kind.AMOUNT: 1.0,
        Kind.RATIO: 0.5,
        Kind.FLAG: True,
    }
    row = {name: values[KNOWN[spike].metrics[name]] for name in sorted(shape.required)}
    if spike == "ocr":  # no call failed, so no time was lost in one
        row["seconds_spent_in_failures"] = 0.0
    if spike == "embedding":  # a run that got this far embedded something
        row |= dict.fromkeys(("chunks", "dimensions", "batch_size", "vault_chunks_assumed"), 1)
    return row


PRIVATE_TEXT = "PATIENT-DIAGNOSIS-CONFIDENTIAL"


class FakeEmbedder:
    name = "qwen3-embedding-0.6b-cpu"
    loaded = False

    def load(self) -> None:
        FakeEmbedder.loaded = True

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class FakeRapid:
    name = "rapidocr-ppocrv5"

    def load(self) -> None:
        pass

    def recognise(self, image: Path) -> str:
        return image.read_text(encoding="utf-8")


class CliTest(unittest.TestCase):
    def test_check_lists_the_optional_pieces_with_the_extra_to_install(self) -> None:
        with mock.patch.object(
            cli, "available", return_value={"pypdfium2": False, "tesseract_on_path": False}
        ):
            code, out, _ = run("check")
        self.assertEqual(code, 0)
        self.assertIn("pypdfium2: no   (uv sync --extra pdf)", out)
        self.assertIn("sqlite can load extensions:", out)

    def test_an_ocr_run_writes_a_report_with_numbers_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "images"
            images.mkdir()
            (images / "a.png").write_text(f"{PRIVATE_TEXT} alpha", encoding="utf-8")
            (images / "a.txt").write_text(f"{PRIVATE_TEXT} alpha", encoding="utf-8")
            with mock.patch.object(adapters, "RapidOcrEngine", FakeRapid):
                code, out, err = run(
                    "ocr",
                    "--engine",
                    "rapidocr",
                    "--images",
                    str(images),
                    "--out",
                    str(root / "out"),
                )
            self.assertEqual(code, 0, err)
            written = (root / "out" / "ocr-rapidocr-ppocrv5-private.json").read_text(
                encoding="utf-8"
            )
            markdown = (root / "out" / "ocr-rapidocr-ppocrv5-private.md").read_text(
                encoding="utf-8"
            )
        for text in (written, markdown, out, err):
            self.assertNotIn(PRIVATE_TEXT, text)
        self.assertEqual(json.loads(written)["rows"][0]["metrics"]["cer_mean"], 0.0)

    def test_a_successful_run_does_not_print_the_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "images"
            images.mkdir()
            (images / "a.png").write_text("alpha", encoding="utf-8")
            (images / "a.txt").write_text("alpha", encoding="utf-8")
            private = root / "Clients" / "Patient-Name"
            with mock.patch.object(adapters, "RapidOcrEngine", FakeRapid):
                code, out, err = run(
                    "ocr", "--engine", "rapidocr", "--images", str(images), "--out", str(private)
                )
            self.assertEqual(code, 0, err)
            self.assertTrue((private / "ocr-rapidocr-ppocrv5-private.json").is_file())
        for text in (out, err):
            self.assertNotIn("Patient-Name", text)
            self.assertNotIn("Clients", text)
            self.assertNotIn(folder, text)
        self.assertIn(
            "report: ocr-rapidocr-ppocrv5-private.json and ocr-rapidocr-ppocrv5-private.md", out
        )

    def embedding_run(
        self,
        root: Path,
        *extra: str,
        factory: Callable[..., object] | None = None,
        cited: str = "a.txt",
    ) -> tuple[int, str, str]:
        """One embedding run on a one-document corpus; the questions are the owner's own."""
        (root / "corpus").mkdir(exist_ok=True)
        (root / "corpus" / "a.txt").write_text("alpha beta gamma", encoding="utf-8")
        (root / "corpus" / "manifest.json").write_text(
            json.dumps(
                {
                    "documents": [
                        {"path": "a.txt", "kind": "text", "pages": None, "needs_ocr": False}
                    ]
                }
            ),
            encoding="utf-8",
        )
        (root / "questions.yaml").write_text(
            "questions:\n- {id: q1, question: q, category: lookup, answerable: true,"
            f" sources: [{{path: {cited}, quote: beta}}]}}\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(
                adapters, "SentenceTransformerEmbedder", factory or (lambda *_a: FakeEmbedder())
            ),
            mock.patch.object(adapters, "gpu_name", return_value="NVIDIA RTX 4000 Ada Generation"),
        ):
            return run(
                "embedding",
                "--candidate",
                "qwen3-embedding-0.6b",
                "--corpus",
                str(root / "corpus"),
                "--questions",
                str(root / "questions.yaml"),
                "--out",
                str(root / "out"),
                *extra,
            )

    def test_a_question_that_cites_an_unlisted_document_stops_the_run_before_it_starts(
        self,
    ) -> None:
        class NeverLoaded(FakeEmbedder):
            def load(self) -> None:
                raise AssertionError("the model must not be loaded")

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            code, out, err = self.embedding_run(
                root, cited="SECRET-b.txt", factory=lambda *_a: NeverLoaded()
            )
            written = (root / "out").exists()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("question q1 cites a document the manifest does not list", err)
        self.assertNotIn("SECRET", err)
        self.assertFalse(written)

    def test_an_embedding_run_keeps_its_measurement_on_a_card_the_vocabulary_lacks(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            code, _, err = self.embedding_run(root)
            self.assertEqual(code, 0, err)
            written = json.loads(
                (root / "out" / "embedding-qwen3-embedding-0.6b-cpu-private.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(written["environment"]["gpu"], "unknown")

    def test_the_batch_size_asked_for_reaches_the_model_and_the_report(self) -> None:
        made: list[tuple[object, ...]] = []

        def make(*args: object) -> FakeEmbedder:
            made.append(args)
            return FakeEmbedder()

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            code, _, err = self.embedding_run(root, "--batch-size", "5", factory=make)
            self.assertEqual(code, 0, err)
            written = json.loads(
                (root / "out" / "embedding-qwen3-embedding-0.6b-cpu-private.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(made[0][2], 5)  # the third argument is the batch size
        self.assertEqual(written["rows"][0]["metrics"]["batch_size"], 5)

    def test_the_public_and_the_private_run_of_a_candidate_keep_separate_reports(self) -> None:
        # ADR 0013 needs both, and the same candidate on the same device used to write both to
        # one name, so the second run replaced the first.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.embedding_run(root)
            with mock.patch.object(cli, "is_public_dataset", return_value=True):
                code, _, err = self.embedding_run(root)
            self.assertEqual(code, 0, err)
            names = sorted(path.name for path in (root / "out").glob("*.json"))
            flags = {
                name: json.loads((root / "out" / name).read_text(encoding="utf-8"))["rows"][0][
                    "metrics"
                ]["public_dataset"]
                for name in names
            }
        self.assertEqual(
            flags,
            {
                "embedding-qwen3-embedding-0.6b-cpu-private.json": False,
                "embedding-qwen3-embedding-0.6b-cpu-public.json": True,
            },
        )

    def test_a_run_that_would_replace_a_report_stops_before_it_starts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.embedding_run(root)
            first = root / "out" / "embedding-qwen3-embedding-0.6b-cpu-private.json"
            before = first.read_bytes()
            FakeEmbedder.loaded = False
            code, out, err = self.embedding_run(root)
            self.assertEqual(first.read_bytes(), before)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("already in the output folder", err)
        self.assertNotIn(folder, err)
        # Stopped before the models were loaded, not after the hour they take.
        self.assertFalse(FakeEmbedder.loaded)

    def test_a_tesseract_report_reads_back_and_merges_with_another_ocr_report(self) -> None:
        # The version line is "tesseract 5.5.0": stored whole it fails the read-back check, and
        # merge, which reads reports from disk, could not combine the two engines.
        version_line = mock.Mock(stdout="tesseract 5.5.0\n leptonica-1.84\n", stderr="")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "images"
            images.mkdir()
            (images / "a.png").write_text("alpha", encoding="utf-8")
            (images / "a.txt").write_text("alpha", encoding="utf-8")
            with mock.patch.object(adapters, "RapidOcrEngine", FakeRapid):
                code, _, err = run(
                    "ocr", "--engine", "rapidocr", "--images", str(images), "--out", str(root)
                )
            self.assertEqual(code, 0, err)
            ocr_output = mock.Mock(stdout=b"alpha", stderr=b"")

            def fake_run(command: list[str], **_: object) -> mock.Mock:
                return version_line if "--version" in command else ocr_output

            with (
                mock.patch("shutil.which", return_value="tess"),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                code, _, err = run(
                    "ocr", "--engine", "tesseract", "--images", str(images), "--out", str(root)
                )
            self.assertEqual(code, 0, err)
            stored = json.loads((root / "ocr-tesseract-private.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["environment"]["packages"]["tesseract"], "5.5.0")
            code, _, err = run(
                "merge",
                str(root / "ocr-rapidocr-ppocrv5-private.json"),
                str(root / "ocr-tesseract-private.json"),
                "--out",
                str(root / "merged"),
            )
            self.assertEqual(code, 0, err)
            merged = json.loads((root / "merged" / "ocr.json").read_text(encoding="utf-8"))
        self.assertEqual(len(merged["rows"]), 2)

    def test_only_the_repositorys_own_files_are_called_the_public_dataset(self) -> None:
        corpus = cli.REPO_ROOT / "demo" / "corpus"
        questions = cli.REPO_ROOT / "eval" / "public" / "questions.yaml"
        self.assertTrue(cli.is_public_dataset(corpus, questions))
        self.assertTrue(cli.is_public_dataset(corpus / ".." / "corpus", questions))
        with tempfile.TemporaryDirectory() as folder:
            elsewhere = Path(folder)
            self.assertFalse(cli.is_public_dataset(elsewhere, questions))
            self.assertFalse(cli.is_public_dataset(corpus, elsewhere / "questions.yaml"))
            # A copy that merely has the same names is the owner's data until proven otherwise.
            (elsewhere / "demo" / "corpus").mkdir(parents=True)
            self.assertFalse(cli.is_public_dataset(elsewhere / "demo" / "corpus", questions))

    def test_merge_combines_candidate_reports(self) -> None:
        env = Environment("Windows 11", "3.12", "cpu", 1, 1.0, None, {})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = []
            for name in ("qwen3-embedding-0.6b", "embeddinggemma-300m"):
                report = Report("embedding", *KNOWN["embedding"].text, env)
                report.add(f"{name}-cpu", **complete_row("embedding", f"{name}-cpu"))
                paths.append(str(report.write(root, stem=f"embedding-{name}")[0]))
            code, _, err = run("merge", *paths, "--out", str(root / "merged"))
            self.assertEqual(code, 0, err)
            merged = json.loads((root / "merged" / "embedding.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [r["candidate"] for r in merged["rows"]],
            ["qwen3-embedding-0.6b-cpu", "embeddinggemma-300m-cpu"],
        )

    def test_sizes_that_cannot_work_are_refused_before_anything_runs(self) -> None:
        for flag in ("--batch-size", "--vault-chunks"):
            for value in ("0", "-4", "many", "1.5"):
                with self.subTest(flag=flag, value=value), self.assertRaises(SystemExit) as caught:
                    run("embedding", "--candidate", "qwen3-embedding-0.6b", flag, value)
                self.assertEqual(caught.exception.code, 2)
        for flag in ("--dim", "--queries"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                run("vec", flag, "0")
        with self.assertRaises(SystemExit):
            run("vec", "--sizes", "100", "0")

    def test_merge_can_be_repeated_but_never_replaces_one_of_its_inputs(self) -> None:
        env = Environment("Windows 11", "3.12", "cpu", 1, 1.0, None, {})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = Report("embedding", *KNOWN["embedding"].text, env)
            report.add(
                "qwen3-embedding-0.6b-cpu", **complete_row("embedding", "qwen3-embedding-0.6b-cpu")
            )
            source = str(report.write(root / "in", stem="embedding-a")[0])
            for _ in range(2):  # the second merge replaces the first, which is derived data
                code, _, err = run("merge", source, "--out", str(root / "merged"))
                self.assertEqual(code, 0, err)
            # A measurement that carries the merge's own name must survive a merge into its folder.
            own = report.write(root / "own", stem="embedding")[0]
            before = own.read_bytes()
            code, out, err = run("merge", str(own), "--out", str(root / "own"))
            self.assertEqual((code, out), (2, ""))
            self.assertIn("would overwrite one of the reports", err)
            self.assertEqual(own.read_bytes(), before)

    def test_merge_refuses_a_report_with_text_of_its_own(self) -> None:
        env = Environment("Windows 11", "3.12", "cpu", 1, 1.0, None, {})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = Report("embedding", "Patient diagnosis", "Q?", "How.", env)
            report.add(
                "qwen3-embedding-0.6b-cpu", **complete_row("embedding", "qwen3-embedding-0.6b-cpu")
            )
            path = str(report.write(root, stem="embedding-a")[0])
            code, out, err = run("merge", path, "--out", str(root / "merged"))
            self.assertFalse((root / "merged").exists())
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn("Patient", err)

    def test_merge_refuses_names_that_only_look_harmless(self) -> None:
        text = "patient-diagnosis-confidential"
        good = Environment("Windows 11", "3.12", "cpu", 1, 1.0, None, {})
        whole = complete_row("embedding", "qwen3-embedding-0.6b-cpu")
        cases = {
            "candidate": (good, text, whole),
            "metric": (good, "qwen3-embedding-0.6b-cpu", {**whole, "medical_record_number": 1}),
            "package": (
                Environment("Windows 11", "3.12", "cpu", 1, 1.0, None, {text: "1.0"}),
                "qwen3-embedding-0.6b-cpu",
                whole,
            ),
        }
        for kind, (env, candidate, metrics) in cases.items():
            with tempfile.TemporaryDirectory() as folder, self.subTest(kind):
                root = Path(folder)
                report = Report("embedding", *KNOWN["embedding"].text, env)
                report.add(candidate, **metrics)
                path = str(report.write(root, stem="embedding-x")[0])
                code, out, err = run("merge", path, "--out", str(root / "merged"))
                self.assertFalse((root / "merged").exists())
            self.assertEqual((code, out), (2, ""))
            self.assertIn(f"{kind} name", err)
            self.assertNotIn("patient", err)
            self.assertNotIn("medical", err)

    def test_fetch_model_downloads_through_the_adapter(self) -> None:
        with mock.patch.object(adapters, "fetch_model") as fetch:
            code, out, err = run("fetch-model", "--candidate", "qwen3-embedding-0.6b")
        self.assertEqual((code, err), (0, ""))
        fetch.assert_called_once_with("qwen3-embedding-0.6b")
        self.assertEqual(out.strip(), "qwen3-embedding-0.6b: downloaded")

    def test_errors_are_one_line_on_stderr_with_exit_code_two(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            code, out, err = run("pdf", "--extractor", "pypdfium2", "--pdfs", folder)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error: no PDF files"))

    def assert_no_detail(self, code: int, out: str, err: str, *kinds: str) -> None:
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertTrue(any(f"the run stopped with {kind};" in err for kind in kinds), err)
        self.assertEqual(len(err.strip().splitlines()), 1)  # one line, never a traceback
        for leaked in (PRIVATE_TEXT, "SECRET-FOLDER", "Traceback", ".png", ".pdf"):
            self.assertNotIn(leaked, err)

    def test_a_missing_input_folder_does_not_print_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = str(Path(folder) / "SECRET-FOLDER")
            with mock.patch.object(adapters, "RapidOcrEngine", FakeRapid):
                ocr = run("ocr", "--engine", "rapidocr", "--images", missing)
            pdf = run("pdf", "--extractor", "pypdfium2", "--pdfs", missing)
        self.assert_no_detail(*ocr, "FileNotFoundError")
        self.assert_no_detail(*pdf, "FileNotFoundError")

    def test_an_unreadable_output_folder_does_not_print_its_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "SECRET-FOLDER"
            blocker.write_text("a file where the report folder should be", encoding="utf-8")
            code, out, err = run("vec", "--out", str(blocker / "inside"), "--sizes", "10")
        # The exact error depends on the system: a file in the way is a "not a directory" error on
        # Linux and may be another OSError on Windows.
        self.assert_no_detail(
            code, out, err, "NotADirectoryError", "FileNotFoundError", "FileExistsError"
        )

    def test_a_librarys_error_message_is_never_printed(self) -> None:
        class Leaky(FakeRapid):
            def load(self) -> None:
                raise RuntimeError(f"cannot read {PRIVATE_TEXT}.png in SECRET-FOLDER")

        with tempfile.TemporaryDirectory() as folder:
            images = Path(folder) / "images"
            images.mkdir()
            (images / "a.png").write_text("x", encoding="utf-8")
            (images / "a.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(adapters, "RapidOcrEngine", Leaky):
                result = run("ocr", "--engine", "rapidocr", "--images", str(images))
        self.assert_no_detail(*result, "RuntimeError")

    def test_a_plain_value_error_from_a_library_is_not_printed_either(self) -> None:
        # int("...") and json.loads quote their input, so a ValueError is not one of ours by type.
        class Leaky(FakeRapid):
            def load(self) -> None:
                int(f"{PRIVATE_TEXT} SECRET-FOLDER")

        with tempfile.TemporaryDirectory() as folder:
            images = Path(folder)
            (images / "a.png").write_text("x", encoding="utf-8")
            (images / "a.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(adapters, "RapidOcrEngine", Leaky):
                result = run("ocr", "--engine", "rapidocr", "--images", str(images))
        self.assert_no_detail(*result, "ValueError")

    def test_this_packages_own_errors_are_still_explained(self) -> None:
        code, _, err = run("embedding", "--candidate", "qwen3-embedding-0.6b", "--questions", "no")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "))

    def test_missing_inputs_are_named_before_any_model_is_touched(self) -> None:
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(adapters, "SentenceTransformerEmbedder") as embedder,
        ):
            code, out, err = run(
                "embedding",
                "--candidate",
                "qwen3-embedding-0.6b",
                "--corpus",
                folder,
                "--questions",
                str(Path(folder) / "nope.yaml"),
            )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("cannot find the corpus manifest", err)
        self.assertIn("and the eval questions", err)
        self.assertIn("demo corpus change", err)
        embedder.assert_not_called()

    def test_only_the_missing_input_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "manifest.json").write_text("{}", encoding="utf-8")
            code, _, err = run(
                "embedding",
                "--candidate",
                "qwen3-embedding-0.6b",
                "--corpus",
                folder,
                "--questions",
                str(Path(folder) / "nope.yaml"),
            )
        self.assertEqual(code, 2)
        self.assertNotIn("manifest", err.split(". The public")[0])
        self.assertIn("the eval questions", err)

    def test_unknown_candidates_are_refused_by_the_parser(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["embedding", "--candidate", "nonsense"])


class FakePdfExtractor:
    name = "pypdfium2"
    detects_headings = False

    def extract(self, path: Path) -> pdf.PdfText:
        return pdf.PdfText(["page text"], [])


class DatasetNameTest(unittest.TestCase):
    """The OCR and PDF spikes run on the public synthetic files and on the owner's own, and ADRs
    0014 and 0015 need both, so each report says which it measured and has its own file name."""

    def test_only_the_public_corpus_and_what_lies_inside_it_is_public(self) -> None:
        corpus = cli.REPO_ROOT / "demo" / "corpus"
        self.assertTrue(cli.is_public_folder(corpus))
        self.assertTrue(cli.is_public_folder(corpus / "scans", corpus / ".." / "corpus" / "notes"))
        with tempfile.TemporaryDirectory() as folder:
            elsewhere = Path(folder)
            self.assertFalse(cli.is_public_folder(elsewhere))
            # Every folder must be public: the images may be, and the references the owner's own.
            self.assertFalse(cli.is_public_folder(corpus, elsewhere))
            self.assertFalse(cli.is_public_folder(elsewhere, corpus))
        self.assertFalse(cli.is_public_folder(corpus / ".."))
        self.assertFalse(cli.is_public_folder(cli.REPO_ROOT / "demo" / "corpus-copy"))
        self.assertFalse(cli.is_public_folder(corpus.parent))

    def run_both(self, command: list[str], patches: list[mock._patch[object]]) -> Path:
        """Run once on a folder inside a stand-in repository and once outside it, into one
        output folder, and return that folder."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        public = root / "repo" / "demo" / "corpus" / "samples"
        private = root / "owner" / "samples"
        for samples in (public, private):
            samples.mkdir(parents=True)
            for name, body in (("a.png", "alpha"), ("a.txt", "alpha"), ("a.pdf", "alpha")):
                (samples / name).write_text(body, encoding="utf-8")
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(cli, "REPO_ROOT", root / "repo"))
            for samples in (public, private):
                code, _, err = run(*command, str(samples), "--out", str(root / "out"))
                self.assertEqual(code, 0, err)
        return root / "out"

    def test_ocr_runs_on_both_kinds_of_data_are_kept_apart(self) -> None:
        out = self.run_both(
            ["ocr", "--engine", "rapidocr", "--images"],
            [mock.patch.object(adapters, "RapidOcrEngine", FakeRapid)],
        )
        flags = {}
        for kind in ("public", "private"):
            data = json.loads((out / f"ocr-rapidocr-ppocrv5-{kind}.json").read_text("utf-8"))
            flags[kind] = data["rows"][0]["metrics"]["public_dataset"]
        self.assertEqual(flags, {"public": True, "private": False})
        code, _, err = run(
            "merge",
            str(out / "ocr-rapidocr-ppocrv5-public.json"),
            str(out / "ocr-rapidocr-ppocrv5-private.json"),
            "--out",
            str(out / "merged"),
        )
        self.assertEqual(code, 0, err)
        merged = json.loads((out / "merged" / "ocr.json").read_text("utf-8"))
        self.assertEqual(
            [row["metrics"]["public_dataset"] for row in merged["rows"]], [True, False]
        )

    def test_pdf_runs_on_both_kinds_of_data_are_kept_apart(self) -> None:
        out = self.run_both(
            ["pdf", "--extractor", "pypdfium2", "--pdfs"],
            [mock.patch.object(adapters, "Pypdfium2Extractor", FakePdfExtractor)],
        )
        flags = {}
        for kind in ("public", "private"):
            data = json.loads((out / f"pdf-pypdfium2-{kind}.json").read_text("utf-8"))
            flags[kind] = data["rows"][0]["metrics"]["public_dataset"]
        self.assertEqual(flags, {"public": True, "private": False})

    def test_a_row_without_its_dataset_flag_is_refused_on_merge(self) -> None:
        for spike, candidate in (("ocr", "tesseract"), ("pdf", "pypdfium2")):
            with self.subTest(spike=spike), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                row = complete_row(spike, candidate)
                del row["public_dataset"]
                report = Report(spike, *KNOWN[spike].text, MergeRowShapeTest.ENV)
                report.add(candidate, **row)
                path = str(report.write(root, stem=f"{spike}-x")[0])
                code, _, err = run("merge", path, "--out", str(root / "merged"))
                self.assertEqual(code, 2)
                self.assertIn("without metrics", err)


class MergeRowShapeTest(unittest.TestCase):
    """What a merge accepts of a row is fixed per candidate, so ADR evidence cannot lose the
    context a figure needs (what it was measured on, how much was left out) or carry a figure the
    candidate cannot produce."""

    ENV = Environment("Windows 11", "3.12", "cpu", 1, 1.0, None, {})

    def merge(self, spike: str, candidate: str, metrics: dict[str, float]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            report = Report(spike, *KNOWN[spike].text, self.ENV)
            report.add(candidate, **metrics)
            path = str(report.write(root, stem=f"{spike}-x")[0])
            code, _, err = run("merge", path, "--out", str(root / "merged"))
        return code, err

    def test_every_candidate_merges_with_its_whole_row(self) -> None:
        for spike, candidate in (
            ("embedding", "embeddinggemma-300m-cpu"),
            ("embedding", "qwen3-embedding-0.6b-cuda"),
            ("ocr", "rapidocr-ppocrv5"),
            ("ocr", "tesseract"),
            ("pdf", "pypdfium2"),
            ("pdf", "pypdfium2+pdfplumber"),
            ("vec", "extension-loading"),
            ("vec", "exact-search-check"),
            ("vec", "vectors-10000"),
        ):
            with self.subTest(candidate=candidate):
                code, err = self.merge(spike, candidate, complete_row(spike, candidate))
                self.assertEqual((code, err), (0, ""))

    def test_a_row_without_its_context_is_refused(self) -> None:
        for spike, candidate, lost in (
            ("embedding", "qwen3-embedding-0.6b-cpu", "public_dataset"),
            ("embedding", "qwen3-embedding-0.6b-cpu", "documents_skipped"),
            ("ocr", "tesseract", "failures"),
            ("pdf", "pypdfium2", "files_ok"),
            ("vec", "extension-loading", "vec_loads"),
        ):
            with self.subTest(candidate=candidate, lost=lost):
                row = complete_row(spike, candidate)
                del row[lost]
                code, err = self.merge(spike, candidate, row)
                self.assertEqual(code, 2)
                self.assertIn("without metrics", err)
                self.assertNotIn(lost, err)

    def test_an_empty_row_is_refused(self) -> None:
        for spike, candidate in (("embedding", "embeddinggemma-300m-cpu"), ("ocr", "tesseract")):
            with self.subTest(candidate=candidate):
                self.assertEqual(self.merge(spike, candidate, {})[0], 2)

    def test_a_figure_the_candidate_cannot_produce_is_refused(self) -> None:
        for spike, candidate, extra in (
            ("embedding", "qwen3-embedding-0.6b-cpu", "gpu_peak_reserved_mib"),  # no video memory
            ("ocr", "tesseract", "peak_memory_mib"),  # a separate program
            ("pdf", "pypdfium2", "heading_f1"),  # this extractor finds no headings
            ("vec", "extension-loading", "dim"),  # a figure of the size rows
            ("vec", "exact-search-check", "query_ms_p50"),
        ):
            with self.subTest(candidate=candidate, extra=extra):
                code, err = self.merge(
                    spike, candidate, {**complete_row(spike, candidate), extra: 1.0}
                )
                self.assertEqual(code, 2)
                self.assertIn("metric name this candidate cannot produce", err)

    def test_a_figure_a_run_may_lack_is_not_required(self) -> None:
        for spike, candidate, extra in (
            (
                "embedding",
                "qwen3-embedding-0.6b-cuda",
                ("gpu_peak_reserved_mib", "gpu_peak_allocated_mib"),
            ),
            ("ocr", "rapidocr-ppocrv5", ("peak_memory_mib",)),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    self.merge(spike, candidate, complete_row(spike, candidate)), (0, "")
                )
                row = {**complete_row(spike, candidate), **dict.fromkeys(extra, 1.0)}
                self.assertEqual(self.merge(spike, candidate, row), (0, ""))

    def test_half_a_group_of_figures_is_refused(self) -> None:
        scores: dict[str, float | None] = {
            "recall_at_10": 0.5,
            "mrr_at_10": 0.5,
            "hit_at_1_questions": 3,
        }
        reads: dict[str, float | None] = dict.fromkeys(("cer_mean", "cer_median", "cer_p90"), 0.1)
        cases: list[tuple[str, str, dict[str, float | None]]] = [
            ("embedding", "qwen3-embedding-0.6b-cpu", {"recall_at_10": 0.5}),
            ("embedding", "qwen3-embedding-0.6b-cpu", {**scores, "mrr_at_10": None}),
            ("embedding", "qwen3-embedding-0.6b-cuda", {"gpu_peak_reserved_mib": 1.0}),
            ("ocr", "tesseract", reads),
            ("pdf", "pypdfium2", {"wer_mean": 0.1}),
            ("pdf", "pypdfium2+pdfplumber", {"heading_false_positives": 2}),
        ]
        for spike, candidate, part in cases:
            with self.subTest(candidate=candidate, part=sorted(part)):
                joined = {**complete_row(spike, candidate), **part}
                row = {name: value for name, value in joined.items() if value is not None}
                code, err = self.merge(spike, candidate, row)
                self.assertEqual(code, 2)
                self.assertIn("part of a group", err)

    def test_figures_that_contradict_the_counts_are_refused(self) -> None:
        accuracy = {
            "cer_mean": 0.1,
            "cer_median": 0.1,
            "cer_p90": 0.1,
            "seconds_per_image_p50": 1.0,
            "seconds_per_image_p95": 1.0,
        }
        cpu = "qwen3-embedding-0.6b-cpu"
        wer = {"wer_mean": 0.1, "wer_median": 0.1}
        heading = {"heading_false_positives": 0}
        scored = {
            "questions_scored": 5,
            "recall_at_10": 0.5,
            "mrr_at_10": 0.5,
            "hit_at_1_questions": 7,
        }
        cases: list[tuple[str, str, dict[str, float]]] = [
            # Hit@1 is required exactly when find-the-document questions were scored.
            ("embedding", cpu, {**scored, "hit_at_1_questions": 3}),  # a count but no hit_at_1
            ("embedding", cpu, {**scored, "hit_at_1_questions": 0, "hit_at_1": 1.0}),
            ("embedding", cpu, {**scored, "hit_at_1": 1.0}),  # more marked than scored
            # Scores exist exactly when questions were scored.
            ("embedding", cpu, {"recall_at_10": 0.5, "mrr_at_10": 0.5, "hit_at_1_questions": 0}),
            ("embedding", cpu, {"questions_scored": 5}),
            # OCR: accuracy exists exactly when an image was read.
            ("ocr", "tesseract", {"images": 3}),
            ("ocr", "tesseract", {"images": 3, "failures": 4}),
            ("ocr", "tesseract", {"images": 3, "references_unusable": 3, **accuracy}),
            ("ocr", "tesseract", {"images": 3, "empty_results": 4, **accuracy}),
            # PDF: outcomes add up, and error rates need references.
            ("pdf", "pypdfium2", {"files": 3, "files_ok": 2}),
            ("pdf", "pypdfium2", {"files": 3, "files_ok": 3, "references_used": 1}),
            ("pdf", "pypdfium2", {"pages": 1, "pages_without_text": 2}),
            ("pdf", "pypdfium2", {"files": 1, "files_ok": 1, "references_used": 99, **wer}),
            ("pdf", "pypdfium2", {"pages": 3}),  # pages come from files that were read
            ("pdf", "pypdfium2", {"files": 1, "files_ok": 1, "files_with_heading_candidates": 2}),
            (
                "pdf",
                "pypdfium2+pdfplumber",
                {"files": 1, "files_ok": 1, "files_with_heading_reference": 5, **heading},
            ),
            ("pdf", "pypdfium2", {"pages": 0, "pages_per_second": 2.0}),
            # OCR: time is lost in failures only when a call failed.
            ("ocr", "tesseract", {"seconds_spent_in_failures": 4.0}),
            # An embedding row has embedded something.
            ("embedding", cpu, {"chunks": 0}),
            ("embedding", cpu, {"batch_size": 0}),
            # sqlite-vec: an extension cannot load into a Python that cannot load extensions.
            ("vec", "extension-loading", {"can_load_extensions": False, "vec_loads": True}),
            ("vec", "exact-search-check", {"vectors": 0}),
        ]
        for spike, candidate, change in cases:
            with self.subTest(candidate=candidate, change=change):
                row = {**complete_row(spike, candidate), **change}
                code, err = self.merge(spike, candidate, row)
                self.assertEqual(code, 2)
                self.assertIn("figures do not fit together", err)

    def test_figures_that_agree_with_the_counts_are_accepted(self) -> None:
        cpu = "qwen3-embedding-0.6b-cpu"
        marked = {
            "questions_scored": 5,
            "recall_at_10": 0.5,
            "mrr_at_10": 0.5,
            "hit_at_1_questions": 2,
        }
        read = {"images": 4, "references_unusable": 1, "failures": 1, "empty_results": 1}
        cases: list[tuple[str, str, dict[str, float]]] = [
            ("embedding", cpu, {**marked, "hit_at_1": 0.5}),
            ("embedding", cpu, {**marked, "hit_at_1_questions": 0}),
            (
                "ocr",
                "tesseract",
                {**read, "cer_mean_failures_as_errors": 0.5, "cer_mean": 0.2}
                | {
                    "cer_median": 0.2,
                    "cer_p90": 0.3,
                    "seconds_per_image_p50": 1.0,
                    "seconds_per_image_p95": 2.0,
                },
            ),
            ("pdf", "pypdfium2", {"files": 3, "files_ok": 2, "files_corrupt": 1}),
            (
                "pdf",
                "pypdfium2",
                {
                    "files": 1,
                    "files_ok": 1,
                    "references_used": 1,
                    "wer_mean": 0.1,
                    "wer_median": 0.1,
                },
            ),
        ]
        for spike, candidate, change in cases:
            with self.subTest(candidate=candidate, change=change):
                row = {**complete_row(spike, candidate), **change}
                self.assertEqual(self.merge(spike, candidate, row), (0, ""))

    def test_a_whole_group_is_accepted(self) -> None:
        row = {
            **complete_row("embedding", "qwen3-embedding-0.6b-cpu"),
            **{
                "questions_scored": 5,
                "recall_at_10": 0.5,
                "mrr_at_10": 0.5,
                "hit_at_1_questions": 0,
            },
        }
        self.assertEqual(self.merge("embedding", "qwen3-embedding-0.6b-cpu", row), (0, ""))


if __name__ == "__main__":
    unittest.main()
