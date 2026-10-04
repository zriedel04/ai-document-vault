"""Spike 3: OCR accuracy and speed of RapidOCR and Tesseract on labelled images.

Decides ADR 0014. A folder of images, each with a reference text file of the same name, is read by
each engine. Accuracy is the character error rate against the reference; speed is the time per
image. The recognised text and the reference text are compared in memory and never printed or
written: the report holds numbers only. Engines are injected, so the unit tests use a fake.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vault_spikes.corpus import read_owner_text
from vault_spikes.errors import OwnValueError
from vault_spikes.metrics import (
    character_error_rate,
    mean,
    median,
    peak_memory_mib,
    per_second,
    percentile,
)
from vault_spikes.report import (
    ROUNDING,
    Environment,
    Kind,
    Number,
    Report,
    RowShape,
    collect_environment,
    rounded_within,
)

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"})
TITLE = "OCR engines on this PC"
QUESTION = (
    "How accurate (character error rate against reference text) and how fast is each OCR engine "
    "on screenshots and scans on this PC, and how reliable is it on Windows? Feeds ADR 0014."
)
METHOD = (
    "`python -m vault_spikes ocr --engine NAME --images DIR`, one engine per process so the peak "
    "memory is that engine's. Every image in DIR with a reference text file of the same name is "
    "recognised once. Character error rate is the edit distance between the recognised and the "
    "reference text (whitespace collapsed) divided by the reference length; time is wall-clock per "
    "image after one untimed warm-up image. RapidOCR downloads its model files the first time "
    "it is loaded, so they are fetched beforehand with `fetch-model --candidate "
    "rapidocr-ppocrv5`, and `load_seconds` includes a download only if that step was skipped. "
    "Images per minute counts the time lost in failed "
    "calls as well (a timeout can take minutes), and a second error figure counts each failed "
    "image as a wrong one. Peak memory is this process's working set, so it is reported only "
    "for an engine that runs inside it (RapidOCR); Tesseract runs as a separate program per "
    "image and has no memory figure here (read its peak working set in Task Manager while it "
    "runs, if it matters). Recognised text is compared in memory and never stored. The "
    "`public_dataset` metric is true only when the images and the reference texts are both in the "
    "repository's public synthetic corpus (`demo/corpus/`); otherwise they are the owner's own "
    "files, kept outside the repository, and only numbers about them are reported."
)
PACKAGES = ("rapidocr", "onnxruntime", "pillow")
# What a report of this spike may contain (checked when a report is read back for a merge).
# Tesseract is a program, not a package: its version is added by the command line.
REPORT_PACKAGES = frozenset({*PACKAGES, "tesseract"})
METRICS: dict[str, Kind] = {
    "images": Kind.COUNT,
    "failures": Kind.COUNT,
    "empty_results": Kind.COUNT,
    "references_unusable": Kind.COUNT,
    "public_dataset": Kind.FLAG,
    "load_seconds": Kind.AMOUNT,
    "seconds_spent_in_failures": Kind.AMOUNT,
    "images_per_minute": Kind.AMOUNT,
    "cer_mean_failures_as_errors": Kind.AMOUNT,
    "cer_mean": Kind.AMOUNT,
    "cer_median": Kind.AMOUNT,
    "cer_p90": Kind.AMOUNT,
    "seconds_per_image_p50": Kind.AMOUNT,
    "seconds_per_image_p95": Kind.AMOUNT,
    "peak_memory_mib": Kind.AMOUNT,
}
_OCR_REQUIRED = frozenset(
    {
        "images",
        "failures",
        "empty_results",
        "references_unusable",
        "public_dataset",
        "load_seconds",
        "seconds_spent_in_failures",
    }
)
# The rates and timings exist only when an image was read (or failed); see run_ocr_spike.
_OCR_OPTIONAL = frozenset(
    {
        "images_per_minute",
        "cer_mean_failures_as_errors",
        "cer_mean",
        "cer_median",
        "cer_p90",
        "seconds_per_image_p50",
        "seconds_per_image_p95",
    }
)
# Read-accuracy and timing figures come together, from the images that were read.
_OCR_READ = frozenset(
    {"cer_mean", "cer_median", "cer_p90", "seconds_per_image_p50", "seconds_per_image_p95"}
)


def _failure_mean_fits(row: Mapping[str, Number], read: int) -> bool:
    """``cer_mean_failures_as_errors`` is the mean over the images read and the failed calls, a
    failed call counting as 1, so it follows from ``cer_mean`` and the counts (1 when every call
    failed). It is checked within the rounding of both figures."""
    if "cer_mean_failures_as_errors" not in row:
        return True
    failures = row["failures"]
    if read == 0:
        low = high = 1.0
    else:
        cer = row["cer_mean"]
        low = ((cer - ROUNDING) * read + failures) / (read + failures)
        high = ((cer + ROUNDING) * read + failures) / (read + failures)
    return rounded_within(row["cer_mean_failures_as_errors"], low, high)


def _read_figures_fit_the_counts(row: Mapping[str, Number]) -> bool:
    """Accuracy figures exist exactly when some image was read: the images, less the references
    that could not be scored and the calls that failed. The failures-as-errors mean also counts
    the failed calls, so it exists when any scored image was tried, and its value follows from the
    mean of the images read. Empty results are among the images read, and a median is never above
    the higher percentile of the same figures. Images per minute needs some OCR work behind it,
    and is above 0 exactly when an image was read."""
    read = int(row["images"] - row["references_unusable"] - row["failures"])
    tried = row["images"] - row["references_unusable"]
    return (
        read >= 0
        and (row["seconds_spent_in_failures"] == 0 or row["failures"] > 0)
        and row["empty_results"] <= read
        and ("cer_mean" in row) == (read > 0)
        and ("cer_mean_failures_as_errors" in row) == (tried > 0)
        and _failure_mean_fits(row, read)
        and (
            "images_per_minute" not in row
            or (tried > 0 and (row["images_per_minute"] > 0) == (read > 0))
        )
        and (
            "cer_median" not in row
            or (
                row["cer_median"] <= row["cer_p90"]
                and row["seconds_per_image_p50"] <= row["seconds_per_image_p95"]
            )
        )
    )


ROWS = (
    # RapidOCR runs inside this process, so its peak memory is this process's own.
    RowShape(
        re.compile(r"rapidocr-ppocrv5"),
        _OCR_REQUIRED,
        _OCR_OPTIONAL | {"peak_memory_mib"},
        (_OCR_READ,),
        _read_figures_fit_the_counts,
        frozenset({"rapidocr", "onnxruntime"}),
    ),
    # Tesseract is a separate program for each image: no figure here stands for it.
    RowShape(
        re.compile(r"tesseract"),
        _OCR_REQUIRED,
        _OCR_OPTIONAL,
        (_OCR_READ,),
        _read_figures_fit_the_counts,
        # The program's own version, added by the command line, is part of the evidence.
        packages=frozenset({"tesseract"}),
    ),
)
"""The metrics of each kind of row. A report read from a file must give every ``required`` one and
nothing outside ``required`` and ``optional``, so a row cannot lose its context or borrow another
candidate's figures."""
CANDIDATES = re.compile("|".join(f"(?:{shape.candidate.pattern})" for shape in ROWS))


class OcrEngine(Protocol):
    """An OCR engine. ``name`` is a label fixed in code."""

    name: str

    def load(self) -> None: ...

    def recognise(self, image: Path) -> str: ...


@dataclass(frozen=True)
class LabelledImage:
    image: Path
    reference: str


def find_labelled_images(
    images_dir: Path, references_dir: Path | None = None
) -> list[LabelledImage]:
    """Images in ``images_dir`` that have a ``<name>.txt`` reference (in ``references_dir`` or next
    to the image). Images without a reference are ignored; an unreadable reference is an error,
    found here, before any image is read. A reference may be UTF-8 with or without a byte order
    mark, or UTF-16 or UTF-32 with one (``corpus.decode_text``)."""
    folder = references_dir or images_dir
    found: list[LabelledImage] = []
    for image in sorted(images_dir.iterdir()):
        if image.suffix.lower() not in IMAGE_SUFFIXES or not image.is_file():
            continue
        reference_file = folder / f"{image.stem}.txt"
        if reference_file.is_file():
            reference = read_owner_text(reference_file, "a reference text file")
            found.append(LabelledImage(image, reference))
    return found


def run_ocr_spike(
    engine: OcrEngine,
    samples: list[LabelledImage],
    *,
    public_dataset: bool = False,
    environment: Environment | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> Report:
    """Measure one engine over ``samples`` and return a report with one row."""
    if not samples:
        msg = "no image has a reference text file"
        raise OwnValueError(msg)
    report = Report(
        "ocr",
        TITLE,
        QUESTION,
        METHOD,
        environment or collect_environment(PACKAGES),
    )
    begin = clock()
    engine.load()
    load_seconds = clock() - begin

    with contextlib.suppress(Exception):  # a failing engine is a result, counted below
        engine.recognise(samples[0].image)  # warm-up, not timed or scored

    error_rates: list[float] = []
    seconds: list[float] = []
    failures = 0
    failed_seconds = 0.0
    empty = 0
    skipped_references = 0
    for sample in samples:
        try:
            character_error_rate(sample.reference, "")  # an empty reference cannot be scored
        except ValueError:
            skipped_references += 1
            continue
        begin = clock()
        try:
            text = engine.recognise(sample.image)
        except Exception:
            # The time a failing call wasted (a timeout can be minutes) counts as work done.
            failed_seconds += clock() - begin
            failures += 1
            continue
        seconds.append(clock() - begin)
        if not text.strip():
            empty += 1
        error_rates.append(character_error_rate(sample.reference, text))

    metrics: dict[str, int | float] = {
        "images": len(samples),
        "failures": failures,
        "empty_results": empty,
        "references_unusable": skipped_references,
        "public_dataset": public_dataset,
        "load_seconds": load_seconds,
        "seconds_spent_in_failures": failed_seconds,
    }
    worked = sum(seconds) + failed_seconds
    if worked > 0:
        # Practical throughput: images read per minute of all OCR work, failed attempts included.
        metrics["images_per_minute"] = per_second(len(seconds), worked) * 60
    if error_rates or failures:
        # A call that failed read nothing, so for this figure it counts as a wrong image. It is
        # reported even when every call failed (1.0), so an engine that never works compares with
        # one that sometimes does.
        metrics["cer_mean_failures_as_errors"] = mean([*error_rates, *[1.0] * failures])
    if error_rates:
        metrics |= {
            "cer_mean": mean(error_rates),
            "cer_median": median(error_rates),
            "cer_p90": percentile(error_rates, 0.9),
            "seconds_per_image_p50": median(seconds),
            "seconds_per_image_p95": percentile(seconds, 0.95),
        }
    # The peak is this process's own. An engine that runs as a separate program (Tesseract) has no
    # figure here rather than the wrapper's, which would sit next to RapidOCR's real one.
    if getattr(engine, "runs_in_process", True) and (peak := peak_memory_mib()) is not None:
        metrics["peak_memory_mib"] = peak
    report.add(engine.name, **metrics)
    return report
