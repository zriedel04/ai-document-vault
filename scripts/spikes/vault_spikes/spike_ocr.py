"""Spike 3: OCR accuracy and speed of RapidOCR and Tesseract on labelled images.

Decides ADR 0014. A folder of images, each with a reference text file of the same name, is read by
each engine. Accuracy is the character error rate against the reference; speed is the time per
image. The recognised text and the reference text are compared in memory and never printed or
written: the report holds numbers only. Engines are injected, so the unit tests use a fake.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vault_spikes.metrics import (
    character_error_rate,
    mean,
    median,
    peak_memory_mib,
    per_second,
    percentile,
)
from vault_spikes.report import Environment, Report, collect_environment

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"})
QUESTION = (
    "How accurate (character error rate against reference text) and how fast is each OCR engine "
    "on screenshots and scans on this PC, and how reliable is it on Windows? Feeds ADR 0014."
)
METHOD = (
    "`python -m vault_spikes ocr --engine NAME --images DIR`, one engine per process so the peak "
    "memory is that engine's. Every image in DIR with a reference text file of the same name is "
    "recognised once. Character error rate is the edit distance between the recognised and the "
    "reference text (whitespace collapsed) divided by the reference length; time is wall-clock per "
    "image after one untimed warm-up image. Recognised text is compared in memory and never "
    "stored. The reference texts and the images stay outside the repository."
)
PACKAGES = ("rapidocr", "onnxruntime", "pillow")


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
    to the image). Images without a reference are ignored; an unreadable reference is an error."""
    folder = references_dir or images_dir
    found: list[LabelledImage] = []
    for image in sorted(images_dir.iterdir()):
        if image.suffix.lower() not in IMAGE_SUFFIXES or not image.is_file():
            continue
        reference_file = folder / f"{image.stem}.txt"
        if reference_file.is_file():
            found.append(LabelledImage(image, reference_file.read_text(encoding="utf-8")))
    return found


def run_ocr_spike(
    engine: OcrEngine,
    samples: list[LabelledImage],
    *,
    environment: Environment | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> Report:
    """Measure one engine over ``samples`` and return a report with one row."""
    if not samples:
        msg = "no image has a reference text file"
        raise ValueError(msg)
    report = Report(
        "ocr",
        "OCR engines on this PC",
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
        "load_seconds": load_seconds,
    }
    if error_rates:
        metrics |= {
            "cer_mean": mean(error_rates),
            "cer_median": median(error_rates),
            "cer_p90": percentile(error_rates, 0.9),
            "seconds_per_image_p50": median(seconds),
            "seconds_per_image_p95": percentile(seconds, 0.95),
            "images_per_minute": per_second(len(seconds), sum(seconds)) * 60,
        }
    if (peak := peak_memory_mib()) is not None:
        metrics["peak_memory_mib"] = peak
    report.add(engine.name, **metrics)
    return report
