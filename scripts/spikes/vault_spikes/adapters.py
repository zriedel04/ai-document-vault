"""The real engines, behind the interfaces the spikes use. Each imports its library when it is
used, so the rest of the package (and CI) works without them.

These adapters are thin on purpose. They were written against the libraries' documented APIs and
their first real run is on the owner's Windows PC (the unit tests use fakes), so ``python -m
vault_spikes check`` reports what is installed before any measurement starts.
"""

from __future__ import annotations

import re
import shutil
import statistics
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from vault_spikes.errors import OwnMessage, OwnRuntimeError, OwnValueError
from vault_spikes.report import MODEL_REVISION, package_version
from vault_spikes.spike_pdf import (
    HEADING_MAX_WORDS,
    HEADING_RATIO,
    ExtractionError,
    Outcome,
    PdfText,
)

VERSION_NUMBER = re.compile(r"\d+(?:\.\d+)+")


class MissingDependencyError(OwnMessage, RuntimeError):
    """An optional library or program is not installed."""


def require_installed(module: str, extra: str) -> None:
    """Stop with a message if ``module`` cannot be imported.

    The message names all the extras, not just this one: ``uv sync --extra X`` makes the
    environment hold exactly what it lists, so it would remove the libraries of the other spikes.
    """
    try:
        __import__(module)
    except ImportError:
        msg = (
            f"{module} is not installed (the {extra} extra); install the spike libraries with: "
            "uv sync --python 3.12 --all-extras"
        )
        raise MissingDependencyError(msg) from None


EMBEDDING_LIBRARIES = ("sentence_transformers", "pypdfium2")


def require_embedding_libraries() -> None:
    """Stop before the corpus is read if a library of the embedding spike is missing.

    pypdfium2 is one of them because the corpus reader reads PDFs with it. Without it, every PDF
    would be skipped as unreadable and the questions on them left out of the scores, so the report
    would compare the candidates on a smaller corpus than the one it names.
    """
    for module in EMBEDDING_LIBRARIES:
        require_installed(module, "embedding")


def require_cuda() -> None:
    """Stop with a clear message if CUDA was asked for but this torch cannot use it.

    The torch wheels that PyPI serves for Windows are CPU-only, so a plain ``uv sync`` gives a
    working install that would otherwise fail deep inside the model loader.
    """
    require_installed("torch", "embedding")
    import torch

    if not torch.cuda.is_available():
        msg = (
            "device cuda was requested but this torch cannot use the GPU. Install the CUDA build "
            "of torch (the command is on pytorch.org, Get Started), then run with "
            "`uv run --no-sync` so uv does not put the CPU build back"
        )
        raise MissingDependencyError(msg)


# --- embeddings -----------------------------------------------------------------------------

EMBEDDING_MODELS = {
    "qwen3-embedding-0.6b": "Qwen/Qwen3-Embedding-0.6B",
    "embeddinggemma-300m": "google/embeddinggemma-300m",
}


DEFAULT_BATCH_SIZE = 16


class SentenceTransformerEmbedder:
    """A sentence-transformers model on the CPU or a CUDA GPU."""

    def __init__(self, candidate: str, device: str, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        if candidate not in EMBEDDING_MODELS:
            msg = f"unknown candidate {candidate!r}; choose one of {sorted(EMBEDDING_MODELS)}"
            raise OwnValueError(msg)
        if device not in {"cpu", "cuda"}:
            msg = "device must be cpu or cuda"
            raise OwnValueError(msg)
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            msg = "the batch size must be a whole number of at least 1"
            raise OwnValueError(msg)
        # The size the report records. It is also passed to the encoder, which otherwise batches
        # by its own default (32): a report must name the batch the measurement really used.
        self.batch_size = batch_size
        self.candidate = candidate
        self.name = f"{candidate}-{device}"
        self.model_id = EMBEDDING_MODELS[candidate]
        self.device = device
        self.model: object = None

    def load(self) -> None:
        require_installed("sentence_transformers", "embedding")
        if self.device == "cuda":
            require_cuda()
        from sentence_transformers import SentenceTransformer

        # Only from the local cache, so the timed load never includes a download (which would
        # make the first candidate run look slower than the next ones). ``fetch_model`` fills the
        # cache first and is not timed.
        try:
            self.model = SentenceTransformer(
                self.model_id, device=self.device, local_files_only=True
            )
        except OSError as error:
            msg = (
                f"the model {self.model_id} is not in the local cache yet; download it once with "
                f"`uv run python -m vault_spikes fetch-model --candidate {self.candidate}` "
                "(a download is not timed), then run this command again"
            )
            raise MissingDependencyError(msg) from error

    def _encode(self, texts: Sequence[str], *, query: bool) -> list[list[float]]:
        model = self.model
        if model is None:
            msg = "call load() first"
            raise OwnRuntimeError(msg)
        # Newer sentence-transformers versions have encode_query / encode_document, which apply
        # each model's own prompts. Fall back to a plain encode on older ones.
        method = getattr(model, "encode_query" if query else "encode_document", None)
        encode: Callable[..., object] = method or model.encode  # type: ignore[attr-defined]
        vectors = encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        return vectors.tolist()  # type: ignore[attr-defined, no-any-return]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, query=False)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, query=True)


def fetch_model(candidate: str) -> None:
    """Download a candidate's files into the local cache, outside any measurement."""
    if candidate == RapidOcrEngine.name:
        # RapidOCR downloads its model files the first time it is loaded, and the OCR spike times
        # the load. Loading it once here keeps the download out of that time.
        RapidOcrEngine().load()
        return
    if candidate not in EMBEDDING_MODELS:
        msg = f"unknown candidate {candidate!r}; choose one of {sorted(EMBEDDING_MODELS)}"
        raise OwnValueError(msg)
    require_installed("sentence_transformers", "embedding")
    from sentence_transformers import SentenceTransformer

    try:
        SentenceTransformer(EMBEDDING_MODELS[candidate], device="cpu")
    except OSError as error:
        msg = (
            f"could not download {EMBEDDING_MODELS[candidate]}: check the network connection. A "
            "model that asks you to accept its licence also needs `uv run hf auth login`"
        )
        raise MissingDependencyError(msg) from error


def model_revision(candidate: str) -> str | None:
    """The Git commit of the cached files of a candidate model, which is what the next load reads
    (the cache's reference to the default branch), or None when it cannot be found.

    A model repository's default branch moves, and ``fetch-model`` follows it every time it is
    run, so the report records the commit that was really loaded and two rows made with different
    weights or prompts can be told apart. The cache names each snapshot folder by its commit.
    """
    if candidate not in EMBEDDING_MODELS:
        return None
    try:
        from huggingface_hub import snapshot_download

        folder = Path(snapshot_download(EMBEDDING_MODELS[candidate], local_files_only=True))
    except Exception:  # any failure to look is "unknown": the run is already done
        return None
    return folder.name if MODEL_REVISION.fullmatch(folder.name) else None


def gpu_name() -> str | None:
    """The name of the first CUDA GPU, or None if there is none (or torch is not installed)."""
    try:
        import torch
    except ImportError:
        return None
    return str(torch.cuda.get_device_name(0)) if torch.cuda.is_available() else None


# --- OCR ------------------------------------------------------------------------------------


class RapidOcrEngine:
    name = "rapidocr-ppocrv5"
    runs_in_process = True

    def __init__(self) -> None:
        self.engine: Callable[[str], object] | None = None

    def load(self) -> None:
        require_installed("rapidocr", "ocr")
        require_installed("onnxruntime", "ocr")  # the engine RapidOCR runs its models on
        from rapidocr import ModelType, OCRVersion, RapidOCR

        # The package default is not PP-OCRv5 (it is PP-OCRv4 before 3.9 and PP-OCRv6 from 3.9), so
        # the detection and recognition models are named here. Otherwise the numbers below would
        # be another model's, reported under this model's name. Classification stays the default.
        self.engine = RapidOCR(
            params={
                "Det.ocr_version": OCRVersion.PPOCRV5,
                "Det.model_type": ModelType.MOBILE,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
            }
        )

    def recognise(self, image: Path) -> str:
        if self.engine is None:
            msg = "call load() first"
            raise OwnRuntimeError(msg)
        result = self.engine(str(image))
        texts = getattr(result, "txts", None)  # rapidocr 3.x: an object with .txts
        return "\n".join(texts) if texts else ""


class TesseractEngine:
    """Tesseract through its command line: no Python package, but a separate Windows install."""

    name = "tesseract"
    # Each image is read by a separate tesseract process, so this process's memory is the small
    # wrapper's and says nothing about the engine; the OCR spike leaves the figure out.
    runs_in_process = False

    def __init__(self, executable: str | None = None, language: str = "eng") -> None:
        self.executable = executable
        self.language = language

    def load(self) -> None:
        found = self.executable or shutil.which("tesseract")
        if not found:
            msg = "tesseract is not installed or not on PATH; pass --tesseract-path"
            raise MissingDependencyError(msg)
        self.executable = found

    def version(self) -> str:
        """The version number in the first line of ``tesseract --version`` (``5.5.0`` from
        ``tesseract 5.5.0``), or ``unknown``. A report holds a bare version, not the whole line."""
        self.load()
        done = subprocess.run(  # noqa: S603 - a fixed program and argument list, no shell
            [str(self.executable), "--version"], capture_output=True, text=True, check=True
        )
        lines = (done.stdout or done.stderr).splitlines()
        found = VERSION_NUMBER.search(lines[0]) if lines else None
        return package_version(found.group(0)) if found else "unknown"

    def recognise(self, image: Path) -> str:
        if self.executable is None:
            msg = "call load() first"
            raise OwnRuntimeError(msg)
        done = subprocess.run(  # noqa: S603 - a fixed program and argument list, no shell
            [self.executable, str(image), "stdout", "-l", self.language],
            capture_output=True,
            check=True,
            timeout=120,
        )
        return done.stdout.decode("utf-8", errors="replace")


# --- PDF ------------------------------------------------------------------------------------


def _classify(error: Exception) -> Outcome:
    """Map a library error to an outcome by its text. The text is never stored or reported."""
    message = str(error).lower()
    if "password" in message or "encrypt" in message:
        return Outcome.ENCRYPTED
    if any(word in message for word in ("format", "corrupt", "syntax", "eof", "trailer", "xref")):
        return Outcome.CORRUPT
    return Outcome.ERROR


class Pypdfium2Extractor:
    """Page text from pypdfium2; no heading detection."""

    name = "pypdfium2"
    detects_headings = False

    def load(self) -> None:
        require_installed("pypdfium2", "pdf")

    def extract(self, path: Path) -> PdfText:
        require_installed("pypdfium2", "pdf")
        import pypdfium2

        try:
            document = pypdfium2.PdfDocument(str(path))
        except Exception as error:
            raise ExtractionError(_classify(error)) from None
        try:
            pages: list[str] = []
            for index in range(len(document)):
                page = document[index]
                textpage = page.get_textpage()
                pages.append(textpage.get_text_range())
                textpage.close()
                page.close()
            return PdfText(pages, [])
        except Exception as error:
            raise ExtractionError(_classify(error)) from None
        finally:
            document.close()


def heading_candidates(words: list[dict[str, float | str]]) -> list[str]:
    """The lines of at most ``HEADING_MAX_WORDS`` words that are all well above the median size.

    ``words`` are pdfplumber words with ``top``, ``size`` and ``text``. Words whose tops differ by
    less than two points share a line, and a line reads left to right.
    """
    if not words:
        return []
    median_size = statistics.median(float(w["size"]) for w in words)
    lines: list[list[dict[str, float | str]]] = []
    for word in sorted(words, key=lambda w: float(w["top"])):
        if lines and abs(float(lines[-1][0]["top"]) - float(word["top"])) < 2:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [
        " ".join(str(w["text"]) for w in sorted(line, key=lambda w: float(w.get("x0", 0.0))))
        for line in lines
        if len(line) <= HEADING_MAX_WORDS
        and all(float(w["size"]) >= HEADING_RATIO * median_size for w in line)
    ]


class PypdfiumPlumberExtractor:
    """Page text from pypdfium2, and heading candidates from pdfplumber's font sizes."""

    name = "pypdfium2+pdfplumber"
    detects_headings = True

    def load(self) -> None:
        require_installed("pypdfium2", "pdf")
        require_installed("pdfplumber", "pdf")

    def extract(self, path: Path) -> PdfText:
        require_installed("pdfplumber", "pdf")
        import pdfplumber

        text = Pypdfium2Extractor().extract(path)
        try:
            with pdfplumber.open(str(path)) as document:
                headings = [
                    heading
                    for page in document.pages
                    for heading in heading_candidates(page.extract_words(extra_attrs=["size"]))
                ]
        except Exception as error:
            raise ExtractionError(_classify(error)) from None
        return PdfText(text.pages, headings)


def pdf_pages_for_corpus(path: Path) -> list[str] | None:
    """Page texts for the embedding spike's corpus reader (text PDFs only).

    None for a PDF that is encrypted, corrupt or otherwise unreadable, so the corpus skips and
    counts it. A missing library is not that: ``MissingDependencyError`` still stops the run.
    """
    try:
        return Pypdfium2Extractor().extract(path).pages
    except ExtractionError:
        return None
