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
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from vault_spikes.metrics import mean, median, peak_memory_mib, per_second, word_error_rate
from vault_spikes.report import Environment, Report, collect_environment

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
    "extracted text with a reference text file of the same name where one exists. Extracted "
    "text is compared in memory and never stored; files and references stay outside the "
    "repository unless they are the public synthetic fixtures."
)
PACKAGES = ("pypdfium2", "pdfplumber")
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
    pages: list[str]
    headings: int


class PdfExtractor(Protocol):
    """Extracts one PDF. ``name`` is a label fixed in code."""

    name: str

    def extract(self, path: Path) -> PdfText: ...


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
        "PDF extraction on this PC",
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
        headings += result.headings
        if result.headings:
            files_with_headings += 1
        reference = (references_dir or pdf.parent) / f"{pdf.stem}.txt"
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
    if (peak := peak_memory_mib()) is not None:
        metrics["peak_memory_mib"] = peak
    report.add(extractor.name, **metrics)
    return report
