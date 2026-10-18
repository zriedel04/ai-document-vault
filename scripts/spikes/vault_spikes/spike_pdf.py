"""Spike 4: PDF text extraction with pypdfium2, and heading detection with pdfplumber.

Decides ADR 0015. For each PDF in a folder the script extracts the text of every page, times it,
classifies failures (encrypted, corrupt, other), counts pages with no text layer (scans), counts
heading candidates by font size, and, if a reference text file of the same name exists, reports the
word error rate. The extracted text is never printed or stored. Extractors are injected, so the
unit tests use a fake and CI needs neither library.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from vault_spikes.corpus import not_a_link, read_owner_text, reference_folder
from vault_spikes.errors import OwnValueError
from vault_spikes.metrics import (
    has_text,
    mean,
    median,
    peak_memory_mib,
    per_second,
    word_error_rate,
)
from vault_spikes.report import (
    ROUNDING,
    Environment,
    Kind,
    Number,
    Report,
    RowShape,
    collect_environment,
    right_share,
    rounded_within,
)

TITLE = "PDF extraction on this PC"
QUESTION = (
    "How fast and how accurate is PDF text extraction with pypdfium2, does heading detection by "
    "font size with pdfplumber work, and how do the libraries behave on encrypted, corrupt and "
    "scanned files on this PC? Feeds ADR 0015."
)
METHOD = (
    "`python -m vault_spikes pdf --extractor NAME --pdfs DIR`. Every PDF in DIR and its "
    "subfolders is extracted page by page. Pages per second is total pages over total extraction "
    "time. A page with no text counts as scanned. Heading candidates are lines whose words are at "
    "least 1.2 times the page's median font size and that have at most 12 words. Word error rate "
    "compares the extracted text with a reference text file of the same name (`NAME.txt`, next "
    "to the PDF or at the same relative path under `--references`) where one exists; "
    "a reference with no words has no error rate and is counted in `references_unusable`. "
    "Heading accuracy compares the detected headings with a file of the expected ones "
    "(`NAME.headings.txt`, one heading per line, case and spacing ignored; a heading is counted "
    "once per occurrence, so a false running header found on 20 pages is 20 errors): precision is "
    "the share of detected headings that are expected, recall the share of expected headings that "
    "were detected, both summed over all files that have such a file; an empty file says the PDF "
    "has no headings, so every heading found in it is a false one. Extracted text and "
    "headings are compared in memory and never stored. The `public_dataset` metric is true only "
    "when the PDFs and the reference files are both in the repository's public synthetic corpus "
    "(`demo/corpus/`); otherwise they are the owner's own files, kept outside the repository, "
    "and only numbers about them are reported."
)
PACKAGES = ("pypdfium2", "pdfplumber")
# What a report of this spike may contain (checked when a report is read back for a merge).
METRICS: dict[str, Kind] = {
    "files": Kind.COUNT,
    "public_dataset": Kind.FLAG,
    "files_ok": Kind.COUNT,
    "files_encrypted": Kind.COUNT,
    "files_corrupt": Kind.COUNT,
    "files_other_error": Kind.COUNT,
    "pages": Kind.COUNT,
    "pages_without_text": Kind.COUNT,
    "heading_candidates": Kind.COUNT,
    "files_with_heading_candidates": Kind.COUNT,
    "references_used": Kind.COUNT,
    "references_unusable": Kind.COUNT,
    "pages_per_second": Kind.AMOUNT,
    "wer_mean": Kind.AMOUNT,
    "wer_median": Kind.AMOUNT,
    "files_with_heading_reference": Kind.COUNT,
    "heading_false_positives": Kind.COUNT,
    "heading_recall": Kind.RATIO,
    "heading_precision": Kind.RATIO,
    "heading_f1": Kind.RATIO,
    "peak_memory_mib": Kind.AMOUNT,
}
_PDF_REQUIRED = frozenset(
    {
        "files",
        "public_dataset",
        "files_ok",
        "files_encrypted",
        "files_corrupt",
        "files_other_error",
        "pages",
        "pages_without_text",
        "heading_candidates",
        "files_with_heading_candidates",
        "references_used",
        "references_unusable",
    }
)
_PDF_OPTIONAL = frozenset({"pages_per_second", "wer_mean", "wer_median", "peak_memory_mib"})
_PDF_HEADING_ACCURACY = frozenset(
    {
        "files_with_heading_reference",
        "heading_false_positives",
        "heading_recall",
        "heading_precision",
        "heading_f1",
    }
)
_PDF_WER = frozenset({"wer_mean", "wer_median"})
# A heading reference gives its file count and the false positives together.
_PDF_HEADING_REFERENCE = frozenset({"files_with_heading_reference", "heading_false_positives"})


def _harmonic_mean(recall: float, precision: float) -> float:
    both = recall + precision
    return 2 * precision * recall / both if both else 0.0


def _heading_scores_fit(row: Mapping[str, Number]) -> bool:
    """What ``run_pdf_spike`` can say about headings, and nothing it cannot.

    The scores exist only after a reference file was read, so any of them needs a positive
    ``files_with_heading_reference``, and so does a false-positive count. Recall needs expected
    headings and precision needs detected ones, and F1 is written when either exists, so F1 is
    there exactly when recall or precision is. F1 is the harmonic mean of the two when both exist
    and 0 when only one does; it was computed before either was rounded, so it is checked against
    the range the stored pair allows. A precision that is missing means nothing was detected, so
    there are no false positives; a precision that is there is the right detections out of a
    whole number of them, ``heading_false_positives`` of which were wrong.
    """
    scores = ("heading_recall", "heading_precision", "heading_f1")
    reference = row.get("files_with_heading_reference", 0)
    if reference < 1:
        return not any(name in row for name in (*scores, "heading_false_positives"))
    recall, precision = row.get("heading_recall"), row.get("heading_precision")
    if ("heading_f1" in row) != (recall is not None or precision is not None):
        return False
    if "heading_f1" in row:
        low = high = 0.0
        if recall is not None and precision is not None:
            # The harmonic mean grows with each of its inputs.
            low = _harmonic_mean(max(recall - ROUNDING, 0.0), max(precision - ROUNDING, 0.0))
            high = _harmonic_mean(min(recall + ROUNDING, 1.0), min(precision + ROUNDING, 1.0))
        if not rounded_within(row["heading_f1"], low, high):
            return False
    if precision is None:
        return row["heading_false_positives"] == 0
    return right_share(precision, int(row["heading_false_positives"]))


def _outcomes_add_up(row: Mapping[str, Number]) -> bool:
    """Every file ends in exactly one outcome, the pages without text are among the pages, heading
    candidates come from some file exactly when there are any, and the word error rates exist
    exactly when references were used. A file's reference is used or unusable, never both."""
    outcomes = (
        row["files_ok"] + row["files_encrypted"] + row["files_corrupt"] + row["files_other_error"]
    )
    return (
        outcomes == row["files"]
        and row["pages_without_text"] <= row["pages"]
        and (row["pages"] == 0 or row["files_ok"] > 0)
        and row["files_with_heading_candidates"] <= min(row["files_ok"], row["heading_candidates"])
        and row["references_used"] <= row["files_ok"]
        and row["references_used"] + row["references_unusable"] <= row["files"]
        and row.get("files_with_heading_reference", 0) <= row["files_ok"]
        and ("pages_per_second" not in row or (row["pages"] > 0 and row["pages_per_second"] > 0))
        and (row["heading_candidates"] > 0) == (row["files_with_heading_candidates"] > 0)
        and ("wer_mean" in row) == (row["references_used"] > 0)
        and _heading_scores_fit(row)
    )


def _no_headings_found(row: Mapping[str, Number]) -> bool:
    """pypdfium2 alone returns no headings, so its rows have no heading candidates."""
    return _outcomes_add_up(row) and row["heading_candidates"] == 0


ROWS = (
    # Only the extractor that detects headings can be scored on them.
    RowShape(
        re.compile(r"pypdfium2"),
        _PDF_REQUIRED,
        _PDF_OPTIONAL,
        (_PDF_WER,),
        _no_headings_found,
        frozenset({"pypdfium2"}),
    ),
    RowShape(
        re.compile(r"pypdfium2\+pdfplumber"),
        _PDF_REQUIRED,
        _PDF_OPTIONAL | _PDF_HEADING_ACCURACY,
        (_PDF_WER, _PDF_HEADING_REFERENCE),
        _outcomes_add_up,
        frozenset({"pypdfium2", "pdfplumber"}),
    ),
)
"""The metrics of each kind of row. A report read from a file must give every ``required`` one and
nothing outside ``required`` and ``optional``, so a row cannot lose its context or borrow another
candidate's figures."""
CANDIDATES = re.compile("|".join(f"(?:{shape.candidate.pattern})" for shape in ROWS))
HEADINGS_SUFFIX = ".headings.txt"
HEADING_RATIO = 1.2
HEADING_MAX_WORDS = 12


class Outcome(Enum):
    OK = "ok"
    ENCRYPTED = "encrypted"
    CORRUPT = "corrupt"
    ERROR = "error"


class ExtractionError(Exception):
    """Raised by an extractor with the outcome it classified, never with document text."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome


@dataclass(frozen=True)
class PdfText:
    """What an extractor returns. ``headings`` are the lines it judged to be headings, kept in
    memory only to be compared with the reference headings; they are never reported."""

    pages: list[str]
    headings: list[str]


class PdfExtractor(Protocol):
    """Extracts one PDF. ``name`` is a label fixed in code."""

    name: str
    detects_headings: bool  # False: the extractor returns no headings, so none are scored

    def load(self) -> None:
        """Check that the libraries are there. It is called once, before any file is tried, so a
        missing library stops the run instead of being counted as a failure of every file."""

    def extract(self, path: Path) -> PdfText: ...


def normalise_heading(text: str) -> str:
    return " ".join(text.split()).casefold()


def heading_counts(text: str) -> Counter[str]:
    """The expected headings of a reference file: one per line, blank lines ignored. A heading
    that occurs on several pages is listed once per occurrence."""
    return Counter(line for line in (normalise_heading(raw) for raw in text.splitlines()) if line)


@dataclass(frozen=True)
class Labels:
    """What the owner wrote down about one PDF: its text (``NAME.txt``) and the headings it has
    (``NAME.headings.txt``), each None when there is no such file. Kept in memory only."""

    text: str | None
    headings: Counter[str] | None


def read_labels(pdf: Path, folder: Path, *, headings: bool) -> Labels:
    """The reference files of one PDF. :func:`run_pdf_spike` reads them all before it measures
    anything, so a file that cannot be read stops the run before the measurement, not halfway
    through it. A file may be UTF-8 with or without a byte order mark, or UTF-16 or UTF-32 with
    one (``corpus.decode_text``); the headings file is read only for an extractor that finds
    headings."""
    text_file = not_a_link(folder / f"{pdf.stem}.txt")
    heading_file = not_a_link(folder / f"{pdf.stem}{HEADINGS_SUFFIX}")
    return Labels(
        read_owner_text(text_file, "a reference text file") if text_file.is_file() else None,
        heading_counts(read_owner_text(heading_file, "a headings file"))
        if headings and heading_file.is_file()
        else None,
    )


def run_pdf_spike(
    extractor: PdfExtractor,
    pdfs: list[Path],
    *,
    public_dataset: bool = False,
    references_dir: Path | None = None,
    pdfs_dir: Path | None = None,
    environment: Environment | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> Report:
    """Measure one extractor over ``pdfs`` and return a report with one row.

    A PDF's reference files are next to it, or, with ``references_dir``, in that folder at the
    same path relative to it as the PDF has to ``pdfs_dir`` (the folder the PDFs were found in,
    see ``corpus.files_below``); without ``pdfs_dir``, directly in ``references_dir``."""
    if not pdfs:
        msg = "no PDF files to measure"
        raise OwnValueError(msg)
    report = Report(
        "pdf",
        TITLE,
        QUESTION,
        METHOD,
        environment or collect_environment(PACKAGES),
    )
    extractor.load()  # outside the per-file handling below: a missing library is not a result
    labels = [
        read_labels(
            pdf,
            reference_folder(pdf, pdfs_dir or pdf.parent, references_dir),
            headings=extractor.detects_headings,
        )
        for pdf in pdfs
    ]
    # A reference text with no words has no error rate. It is counted, whatever its PDF's outcome,
    # so a blank or mistyped file cannot take a sample out of the word error rates unseen.
    references_unusable = sum(
        1 for label in labels if label.text is not None and not has_text(label.text)
    )
    outcomes = dict.fromkeys(Outcome, 0)
    pages_total = 0
    pages_without_text = 0
    headings = 0
    files_with_headings = 0
    seconds = 0.0
    word_error_rates: list[float] = []
    heading_hits = heading_detected = heading_expected = files_with_heading_reference = 0
    for pdf, label in zip(pdfs, labels, strict=True):
        begin = clock()
        try:
            result = extractor.extract(pdf)
        except ExtractionError as error:
            seconds += clock() - begin
            outcomes[error.outcome] += 1
            continue
        except Exception:
            seconds += clock() - begin
            outcomes[Outcome.ERROR] += 1
            continue
        seconds += clock() - begin
        outcomes[Outcome.OK] += 1
        pages_total += len(result.pages)
        pages_without_text += sum(1 for page in result.pages if not page.strip())
        headings += len(result.headings)
        if result.headings:
            files_with_headings += 1
        if label.headings is not None:
            expected = label.headings
            detected = Counter(h for h in map(normalise_heading, result.headings) if h)
            # An empty file is a label too: this PDF has no headings, so every heading found in
            # it is a false one and must lower the precision.
            files_with_heading_reference += 1
            # One-to-one: a heading detected on 20 pages is 20 detections, and only as many of them
            # are right as the reference lists occurrences (a false running header is 20 errors).
            heading_hits += (expected & detected).total()
            heading_detected += detected.total()
            heading_expected += expected.total()
        if label.text is not None and has_text(label.text):
            word_error_rates.append(word_error_rate(label.text, "\n".join(result.pages)))

    metrics: dict[str, int | float] = {
        "files": len(pdfs),
        "public_dataset": public_dataset,
        "files_ok": outcomes[Outcome.OK],
        "files_encrypted": outcomes[Outcome.ENCRYPTED],
        "files_corrupt": outcomes[Outcome.CORRUPT],
        "files_other_error": outcomes[Outcome.ERROR],
        "pages": pages_total,
        "pages_without_text": pages_without_text,
        "heading_candidates": headings,
        "files_with_heading_candidates": files_with_headings,
        "references_used": len(word_error_rates),
        "references_unusable": references_unusable,
    }
    if pages_total and seconds > 0:
        metrics["pages_per_second"] = per_second(pages_total, seconds)
    if word_error_rates:
        metrics["wer_mean"] = mean(word_error_rates)
        metrics["wer_median"] = median(word_error_rates)
    if files_with_heading_reference:
        metrics["files_with_heading_reference"] = files_with_heading_reference
        metrics["heading_false_positives"] = heading_detected - heading_hits
        # Recall needs expected headings and precision needs detected ones; a figure with nothing
        # to divide is left out, not set to a guess. F1 is 0 when either side has nothing right.
        recall = heading_hits / heading_expected if heading_expected else None
        precision = heading_hits / heading_detected if heading_detected else None
        if recall is not None:
            metrics["heading_recall"] = recall
        if precision is not None:
            metrics["heading_precision"] = precision
        if heading_expected or heading_detected:
            metrics["heading_f1"] = (
                _harmonic_mean(recall, precision)
                if recall is not None and precision is not None
                else 0.0
            )
    if (peak := peak_memory_mib()) is not None:
        metrics["peak_memory_mib"] = peak
    report.add(extractor.name, **metrics)
    return report
