"""The retrieval evaluation (``python -m vault.eval``) on synthetic corpora.

The corpus here is built in a temporary folder whose names, ids and words look private
(``Jane-Doe``), so every test that runs the command can check that none of them is printed.
"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import warnings
from pathlib import Path
from typing import Any

import docx
import pytest

from vault import eval as cli
from vault.ingest.extractors import NotTextError
from vault.retrieval import evaluation
from vault.retrieval.evaluation import Evaluation, evaluate

PRIVATE = ("Jane", "Doe", "zebra", "aardvark", "pelican", "Patient")
LONG = " ".join(f"zebra{n}" for n in range(20))  # more terms than search takes


def _docx(text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


FILES: dict[str, bytes] = {
    "Jane-Doe-notes/alpha.txt": b"The quarterly zebra report is due Friday.\n",
    "Jane-Doe-notes/beta.md": b"# Beta\n\nThe aardvark budget is forty dollars.\n",
    "Jane-Doe-notes/beta-copy.md": b"# Beta\n\nThe aardvark budget is forty dollars.\n",
    "Jane-Doe-project/spec.docx": _docx("The pelican requirement holds for every release."),
    "Jane-Doe-scans/scan.pdf": b"%PDF-1.4\n%%EOF\n",
    "Jane-Doe-data/table.csv": b"name,hours\nzebra,41\n",
}


def _question(qid: str, text: str, *sources: tuple[str, str], **extra: Any) -> dict[str, Any]:
    return {
        "id": qid,
        "question": text,
        "category": "lookup",
        "answerable": True,
        "answer": "synthetic",
        "sources": [{"path": path, "page": None, "quote": quote} for path, quote in sources],
        **extra,
    }


ALPHA = ("Jane-Doe-notes/alpha.txt", "The quarterly zebra report is due Friday.")
QUESTIONS: list[dict[str, Any]] = [
    _question("Patient-Jane-Doe-1", "zebra report", ALPHA, find_document=True),
    # Cites the copy: an exact duplicate is as good as its original.
    _question(
        "Patient-Jane-Doe-2",
        "aardvark budget",
        ("Jane-Doe-notes/beta-copy.md", "The aardvark budget is forty dollars."),
    ),
    _question(
        "Patient-Jane-Doe-3",
        "pelican requirement",
        ("Jane-Doe-project/spec.docx", "The pelican requirement holds for every release."),
    ),
    _question("Patient-Jane-Doe-4", "zebra aardvark", ALPHA),  # nothing holds both: a miss
    _question("Patient-Jane-Doe-5", LONG, ALPHA),  # search refuses it: a miss
    _question("Patient-Jane-Doe-6", "scan", ("Jane-Doe-scans/scan.pdf", "Scanned zebra.")),
    {
        **_question(
            "Patient-Jane-Doe-7",
            "zebra scan",
            ALPHA,
            ("Jane-Doe-scans/scan.pdf", "Scanned zebra."),
        ),
        "category": "multi_doc",
    },
    {
        "id": "Patient-Jane-Doe-8",
        "question": "zebra salary",
        "category": "not_in_vault",
        "answerable": False,
        "answer": "Not in the vault.",
        "sources": [],
    },
]


def _manifest(duplicate_of: str = "Jane-Doe-notes/beta.md") -> dict[str, object]:
    documents: list[dict[str, object]] = []
    for path in FILES:
        row: dict[str, object] = {"path": path, "kind": path.rsplit(".", 1)[1]}
        if path.endswith("beta-copy.md"):
            row["exact_duplicate_of"] = duplicate_of
        documents.append(row)
    return {"documents": documents}


def _write_corpus(
    root: Path,
    questions: list[dict[str, Any]] | None = None,
    manifest: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    corpus = root / "Jane-Doe-corpus"
    for relative, data in FILES.items():
        (corpus / relative).parent.mkdir(parents=True, exist_ok=True)
        (corpus / relative).write_bytes(data)
    (corpus / "manifest.json").write_text(json.dumps(manifest or _manifest()), encoding="utf-8")
    eval_file = root / "Jane-Doe-questions.yaml"
    # JSON is YAML, so the eval file's loader reads it as it reads questions.yaml.
    eval_file.write_text(
        json.dumps({"questions": QUESTIONS if questions is None else questions}), encoding="utf-8"
    )
    return corpus, eval_file


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    return _write_corpus(tmp_path)


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The system's temporary folder, so a test can see what the run leaves in it."""
    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out + err


def _assert_private(output: str) -> None:
    for word in PRIVATE:
        assert word not in output


def _consistent(result: dict[str, Any]) -> None:
    """The groups of documents and of questions add up to their totals."""
    documents, questions = result["documents"], result["questions"]
    assert documents["listed"] == (
        documents["indexed"]
        + documents["duplicates"]
        + sum(documents["refused"].values())
        + sum(documents["no_extractor"].values())
        + documents["extraction_failed"]
    )
    assert questions["total"] == (
        questions["not_answerable"]
        + questions["scored"]
        + questions["none_indexed"]
        + questions["partly_indexed"]
    )
    assert questions["queries_refused"] <= questions["scored"]


class TestScores:
    def test_the_pipeline_indexes_what_has_an_extractor_and_scores_with_the_spike_rules(
        self, corpus: tuple[Path, Path]
    ) -> None:
        result = evaluate(*corpus)
        documents, questions, scores = result.documents, result.questions, result.scores
        assert documents.listed == 6
        assert documents.indexed == 3  # alpha.txt, beta.md, spec.docx
        assert documents.duplicates == 1  # beta-copy.md is stored as beta.md
        assert documents.no_extractor == {"pdf": 1}
        assert documents.refused == {"unsupported_extension": 1}  # the CSV
        assert documents.extraction_failed == 0
        assert documents.chunks == 3
        assert questions.total == 8
        assert questions.not_answerable == 1
        assert questions.scored == 5
        assert questions.none_indexed == 1  # its only source is the PDF
        assert questions.partly_indexed == 1  # one of its two sources is the PDF
        assert questions.queries_refused == 1
        assert questions.passages_missing == 0
        # Found at rank 1: questions 1 to 3; missed: 4 and 5.
        assert scores.recall_at_10 == pytest.approx(0.6)
        assert scores.mrr_at_10 == pytest.approx(0.6)
        assert scores.hit_at_1 == 1.0
        assert scores.hit_at_1_questions == 1
        _consistent(result.as_json())

    def test_a_passage_no_chunk_holds_is_counted(self, tmp_path: Path) -> None:
        mistyped = [_question("q", "zebra report", (ALPHA[0], "The zebra report is late."))]
        result = evaluate(*_write_corpus(tmp_path, mistyped))
        assert result.questions.passages_missing == 1
        assert result.scores.recall_at_10 == 0.0

    def test_without_a_scorable_question_there_are_no_scores(self, tmp_path: Path) -> None:
        result = evaluate(*_write_corpus(tmp_path, [QUESTIONS[5], QUESTIONS[7]]))
        assert result.questions.scored == 0
        assert (result.scores.recall_at_10, result.scores.mrr_at_10) == (None, None)
        assert result.scores.hit_at_1 is None

    def test_a_document_the_extractor_refuses_is_counted_and_not_indexed(
        self, corpus: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = evaluation.extract

        def refuse_markdown(extension: str, data: bytes) -> Any:
            if extension == "md":
                raise NotTextError
            return real(extension, data)

        monkeypatch.setattr(evaluation, "extract", refuse_markdown)
        result = evaluate(*corpus)
        assert result.documents.extraction_failed == 1
        assert result.documents.indexed == 2
        assert result.questions.none_indexed == 2  # the aardvark question joins the PDF one
        _consistent(result.as_json())

    def test_a_duplicate_mark_the_bytes_do_not_support_stops_the_run(self, tmp_path: Path) -> None:
        corpus = _write_corpus(tmp_path, manifest=_manifest("Jane-Doe-notes/alpha.txt"))
        with pytest.raises(evaluation.EvalError, match="exact duplicate"):
            evaluate(*corpus)

    def test_the_temporary_vault_is_removed(self, corpus: tuple[Path, Path], scratch: Path) -> None:
        evaluate(*corpus)
        assert list(scratch.iterdir()) == []

    def test_the_temporary_vault_is_removed_when_the_run_fails(
        self, corpus: tuple[Path, Path], scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(*_: object) -> None:
            raise RuntimeError

        monkeypatch.setattr(evaluation, "_run", fail)
        with pytest.raises(RuntimeError):
            evaluate(*corpus)
        assert list(scratch.iterdir()) == []

    def test_a_vault_that_cannot_be_removed_is_reported(
        self, corpus: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = tempfile.TemporaryDirectory.cleanup

        def refuse(self: tempfile.TemporaryDirectory[str]) -> None:
            real(self)  # removed all the same, so the test leaves nothing behind
            raise PermissionError

        monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", refuse)
        with pytest.raises(evaluation.EvalError, match="could not be removed"):
            evaluate(*corpus)


class TestCommandLine:
    def test_it_prints_counts_and_scores_and_writes_the_report(
        self, corpus: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = tmp_path / "Jane-Doe-report.json"
        code, output = _run(
            capsys, "--corpus", str(corpus[0]), "--questions", str(corpus[1]), "--json", str(report)
        )
        assert code == 0
        assert "Recall@10: 0.600" in output
        assert "MRR@10: 0.600" in output
        assert "Hit@1: 1.000 over 1 find-the-document questions" in output
        assert "no extractor for the type yet: pdf 1" in output
        assert "refused at import: unsupported_extension 1" in output
        assert "text fallback cp1252" in output
        _assert_private(output)
        written = json.loads(report.read_text(encoding="utf-8"))
        assert written["scores"]["recall_at_10"] == pytest.approx(0.6)
        assert written["questions"]["scored"] == 5
        _consistent(written)
        _assert_private(report.read_text(encoding="utf-8"))
        assert b"\r\n" not in report.read_bytes()

    def test_no_scores_are_printed_as_none_scored(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        corpus, questions = _write_corpus(tmp_path, [QUESTIONS[5]])
        code, output = _run(capsys, "--corpus", str(corpus), "--questions", str(questions))
        assert code == 0
        assert "Recall@10: none scored" in output
        assert "Hit@1: no find-the-document question was scored" in output

    def test_the_public_demo_corpus_runs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        report = tmp_path / "report.json"
        code, _output = _run(capsys, "--json", str(report))
        assert code == 0
        written = json.loads(report.read_text(encoding="utf-8"))
        _consistent(written)
        assert written["questions"]["total"] == 60
        assert written["documents"]["indexed"] > 0
        assert written["documents"]["duplicates"] == 3
        assert set(written["documents"]["no_extractor"]) <= {"pdf", "png", "jpg"}

    def test_an_existing_report_is_refused_before_the_run(
        self,
        corpus: tuple[Path, Path],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report = tmp_path / "Jane-Doe-report.json"
        report.write_text("kept", encoding="utf-8")
        monkeypatch.setattr(evaluation, "evaluate", _never)
        code, output = _run(capsys, "--json", str(report))
        assert code == 1
        assert "already exists" in output
        assert report.read_text(encoding="utf-8") == "kept"
        _assert_private(output)

    def test_a_report_folder_that_cannot_be_written_is_refused_before_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(evaluation, "evaluate", _never)
        code, output = _run(capsys, "--json", str(tmp_path / "Jane-Doe" / "report.json"))
        assert code == 1
        assert "cannot be written" in output
        _assert_private(output)

    def test_a_report_that_appears_during_the_run_is_left_alone(
        self,
        corpus: tuple[Path, Path],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report = tmp_path / "Jane-Doe-report.json"
        real = evaluation.evaluate

        def and_then_a_file(corpus_dir: Path, questions: Path) -> Evaluation:
            result = real(corpus_dir, questions)
            report.write_text("theirs", encoding="utf-8")
            return result

        monkeypatch.setattr(evaluation, "evaluate", and_then_a_file)
        code, output = _run(
            capsys, "--corpus", str(corpus[0]), "--questions", str(corpus[1]), "--json", str(report)
        )
        assert code == 1
        assert "left alone" in output
        assert report.read_text(encoding="utf-8") == "theirs"

    def test_a_report_that_cannot_be_written_is_reported(
        self,
        corpus: tuple[Path, Path],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        report = tmp_path / "Jane-Doe-report.json"
        real = Path.open

        def refuse(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self == report:
                raise PermissionError(str(report))
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", refuse)
        code, output = _run(
            capsys, "--corpus", str(corpus[0]), "--questions", str(corpus[1]), "--json", str(report)
        )
        assert code == 1
        assert "could not be written" in output
        _assert_private(output)

    def test_a_missing_corpus_is_fixed_text(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, output = _run(capsys, "--corpus", str(tmp_path / "Jane-Doe-missing"))
        assert code == 1
        assert "cannot read the manifest" in output
        _assert_private(output)

    def test_a_question_citing_an_unlisted_document_is_named_by_its_number(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        unlisted = [QUESTIONS[0], _question("Patient-x", "zebra", ("Jane-Doe/x.txt", "zebra"))]
        corpus, questions = _write_corpus(tmp_path, unlisted)
        code, output = _run(capsys, "--corpus", str(corpus), "--questions", str(questions))
        assert code == 1
        assert "question number 2" in output
        _assert_private(output)

    def test_an_error_this_package_does_not_own_is_shown_by_its_class_only(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(*_: object) -> None:
            raise FileNotFoundError("/home/Jane-Doe/zebra.txt")

        monkeypatch.setattr(evaluation, "evaluate", fail)
        code, output = _run(capsys)
        assert code == 1
        assert "FileNotFoundError" in output
        _assert_private(output)

    def test_a_temporary_folder_inside_a_repository_is_refused_with_its_fixed_text(
        self,
        corpus: tuple[Path, Path],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        inside = tmp_path / "Jane-Doe-repo"
        (inside / ".git").mkdir(parents=True)
        monkeypatch.setattr(tempfile, "tempdir", str(inside))
        code, output = _run(capsys, "--corpus", str(corpus[0]), "--questions", str(corpus[1]))
        assert code == 1
        assert "inside a Git repository" in output
        _assert_private(output)
        assert [entry.name for entry in inside.iterdir()] == [".git"]  # nothing left behind

    def test_an_interruption_is_fixed_text(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupt(*_: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(evaluation, "evaluate", interrupt)
        code, output = _run(capsys)
        assert code == cli.EXIT_INTERRUPTED
        assert "interrupted" in output

    def test_what_libraries_log_or_warn_is_not_shown(
        self,
        corpus: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real = evaluation.evaluate

        def chatty(corpus_dir: Path, questions: Path) -> Evaluation:
            logging.getLogger("some.library").error("token Jane Doe zebra")
            warnings.warn("Patient Jane Doe", UserWarning, stacklevel=1)
            return real(corpus_dir, questions)

        monkeypatch.setattr(evaluation, "evaluate", chatty)
        code, output = _run(capsys, "--corpus", str(corpus[0]), "--questions", str(corpus[1]))
        assert code == 0
        _assert_private(output)

    @pytest.mark.parametrize(
        "argv",
        [
            ["Jane-Doe-extra"],  # an extra word
            ["--corpus"],  # a missing value
            ["--jsn", "Jane-Doe.json"],  # an unknown option
            ["--cor", "Jane-Doe"],  # an abbreviation
            ["--json", "Jane-Doe.json", "--json"],  # a missing value after a good one
        ],
    )
    def test_a_malformed_command_line_does_not_repeat_what_was_typed(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as stopped:
            cli.main(argv)
        assert stopped.value.code == 2
        out, err = capsys.readouterr()
        assert "command line is not valid" in err
        _assert_private(out + err)


def _never(*_: object) -> None:
    raise AssertionError
