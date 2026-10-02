"""Spike 4: PDF text extraction with pypdfium2, and heading detection with pdfplumber.

Decides ADR 0015. For each PDF in a folder the script extracts the text of every page, times it,
classifies failures (encrypted, corrupt, other), counts pages with no text layer (scans), counts
heading candidates by font size, and, if a reference text file of the same name exists, reports the
word error rate. The extracted text is never printed or stored. Extractors are injected, so the
unit tests use a fake and CI needs neither library.
"""

from __future__ import annotations

import contextlib
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from vault_spikes.metrics import mean, median, peak_memory_mib, per_second, word_error_rate
from vault_spikes.report import Environment, Report, collect_environment

TITLE = "PDF extraction on this PC"
QUESTION = (
    "How fast and how accurate is PDF text extraction with pypdfium2, does heading detection by "
    "font size with pdfplumber work, and how do the libraries behave on encrypted, corrupt and "
    "scanned files on this PC? Feeds ADR 0015."
)
METHOD = (
    "`python -m vault_spikes pdf --extractor NAME --pdfs DIR`. Every PDF in DIR is extracted "
    "page by page. Pages per second is total pages over total extraction time. A page with no "
    "text counts as scanned. Heading candidates are lines whose words are at least 1.2 times the "
    "page's median font size and that have at most 12 words. Word error rate compares the "
    "extracted text with a reference text file of the same name (`NAME.txt`) where one exists. "
    "Heading accuracy compares the detected headings with a file of the expected ones "
    "(`NAME.headings.txt`, one heading per line, case and spacing ignored; a heading is counted "
    "once per occurrence, so a false running header found on 20 pages is 20 errors): precision is "
    "the share of detected headings that are expected, recall the share of expected headings that "
    "were detected, both summed over all files that have such a file; an empty file says the PDF "
    "has no headings, so every heading found in it is a false one. Extracted text and "
    "headings are compared in memory and never stored; files and references stay outside the "
    "repository unless they are the public synthetic fixtures."
)
PACKAGES = ("pypdfium2", "pdfplumber")
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

    def extract(self, path: Path) -> PdfText: ...


def normalise_heading(text: str) -> str:
    return " ".join(text.split()).casefold()


def heading_counts(text: str) -> Counter[str]:
    """The expected headings of a reference file: one per line, blank lines ignored. A heading
    that occurs on several pages is listed once per occurrence."""
    return Counter(line for line in (normalise_heading(raw) for raw in text.splitlines()) if line)


def run_pdf_spike(
    extractor: PdfExtractor,
    pdfs: list[Path],
    *,
    references_dir: Path | None = None,
    environment: Environment | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> Report:
    """Measure one extractor over ``pdfs`` and return a report with one row."""
    if not pdfs:
        msg = "no PDF files to measure"
        raise ValueError(msg)
    report = Report(
        "pdf",
        TITLE,
        QUESTION,
        METHOD,
        environment or collect_environment(PACKAGES),
    )
    outcomes = dict.fromkeys(Outcome, 0)
    pages_total = 0
    pages_without_text = 0
    headings = 0
    files_with_headings = 0
    seconds = 0.0
    word_error_rates: list[float] = []
    heading_hits = heading_detected = heading_expected = files_with_heading_reference = 0
    for pdf in pdfs:
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
        reference_dir = references_dir or pdf.parent
        heading_file = reference_dir / f"{pdf.stem}{HEADINGS_SUFFIX}"
        if extractor.detects_headings and heading_file.is_file():
            expected = heading_counts(heading_file.read_text(encoding="utf-8"))
            detected = Counter(h for h in map(normalise_heading, result.headings) if h)
            # An empty file is a label too: this PDF has no headings, so every heading found in
            # it is a false one and must lower the precision.
            files_with_heading_reference += 1
            # One-to-one: a heading detected on 20 pages is 20 detections, and only as many of them
            # are right as the reference lists occurrences (a false running header is 20 errors).
            heading_hits += (expected & detected).total()
            heading_detected += detected.total()
            heading_expected += expected.total()
        reference = reference_dir / f"{pdf.stem}.txt"
        if reference.is_file():
            with contextlib.suppress(ValueError):  # an empty reference is not scored
                word_error_rates.append(
                    word_error_rate(reference.read_text(encoding="utf-8"), "\n".join(result.pages))
                )

    metrics: dict[str, int | float] = {
        "files": len(pdfs),
        "files_ok": outcomes[Outcome.OK],
        "files_encrypted": outcomes[Outcome.ENCRYPTED],
        "files_corrupt": outcomes[Outcome.CORRUPT],
        "files_other_error": outcomes[Outcome.ERROR],
        "pages": pages_total,
        "pages_without_text": pages_without_text,
        "heading_candidates": headings,
        "files_with_heading_candidates": files_with_headings,
        "references_used": len(word_error_rates),
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
                2 * precision * recall / (precision + recall)
                if recall is not None and precision is not None and precision + recall
                else 0.0
            )
    if (peak := peak_memory_mib()) is not None:
        metrics["peak_memory_mib"] = peak
    report.add(extractor.name, **metrics)
    return report
