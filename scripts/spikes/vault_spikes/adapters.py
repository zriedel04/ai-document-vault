"""The real engines, behind the interfaces the spikes use. Each imports its library when it is
used, so the rest of the package (and CI) works without them.

These adapters are thin on purpose. They were written against the libraries' documented APIs and
their first real run is on the owner's Windows PC (the unit tests use fakes), so ``python -m
vault_spikes check`` reports what is installed before any measurement starts.
"""

from __future__ import annotations

import shutil
import statistics
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from vault_spikes.spike_pdf import (
    HEADING_MAX_WORDS,
    HEADING_RATIO,
    ExtractionError,
    Outcome,
    PdfText,
)


class MissingDependencyError(RuntimeError):
    """An optional library or program is not installed."""


def _need(module: str, extra: str) -> None:
    try:
        __import__(module)
    except ImportError:
        msg = f"{module} is not installed; install it with: uv sync --extra {extra}"
        raise MissingDependencyError(msg) from None


def require_cuda() -> None:
    """Stop with a clear message if CUDA was asked for but this torch cannot use it.

    The torch wheels that PyPI serves for Windows are CPU-only, so a plain ``uv sync`` gives a
    working install that would otherwise fail deep inside the model loader.
    """
    _need("torch", "embedding")
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


class SentenceTransformerEmbedder:
    """A sentence-transformers model on the CPU or a CUDA GPU."""

    def __init__(self, candidate: str, device: str) -> None:
        if candidate not in EMBEDDING_MODELS:
            msg = f"unknown candidate {candidate!r}; choose one of {sorted(EMBEDDING_MODELS)}"
            raise ValueError(msg)
        if device not in {"cpu", "cuda"}:
            msg = "device must be cpu or cuda"
            raise ValueError(msg)
        self.candidate = candidate
        self.name = f"{candidate}-{device}"
        self.model_id = EMBEDDING_MODELS[candidate]
        self.device = device
        self.model: object = None

    def load(self) -> None:
        _need("sentence_transformers", "embedding")
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
            raise RuntimeError(msg)
        # Newer sentence-transformers versions have encode_query / encode_document, which apply
        # each model's own prompts. Fall back to a plain encode on older ones.
        method = getattr(model, "encode_query" if query else "encode_document", None)
        encode: Callable[..., object] = method or model.encode  # type: ignore[attr-defined]
        vectors = encode(list(texts), normalize_embeddings=True, convert_to_numpy=True)
        return vectors.tolist()  # type: ignore[attr-defined, no-any-return]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, query=False)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts, query=True)


def fetch_model(candidate: str) -> None:
    """Download a candidate's files into the local cache, outside any measurement."""
    if candidate not in EMBEDDING_MODELS:
        msg = f"unknown candidate {candidate!r}; choose one of {sorted(EMBEDDING_MODELS)}"
        raise ValueError(msg)
    _need("sentence_transformers", "embedding")
    from sentence_transformers import SentenceTransformer

    try:
        SentenceTransformer(EMBEDDING_MODELS[candidate], device="cpu")
    except OSError as error:
        msg = (
            f"could not download {EMBEDDING_MODELS[candidate]}: check the network connection. A "
            "model that asks you to accept its licence also needs `uv run hf auth login`"
        )
        raise MissingDependencyError(msg) from error


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

    def __init__(self) -> None:
        self.engine: Callable[[str], object] | None = None

    def load(self) -> None:
        _need("rapidocr", "ocr")
        from rapidocr import RapidOCR

        self.engine = RapidOCR()

    def recognise(self, image: Path) -> str:
        if self.engine is None:
            msg = "call load() first"
            raise RuntimeError(msg)
        result = self.engine(str(image))
        texts = getattr(result, "txts", None)  # rapidocr 3.x: an object with .txts
        return "\n".join(texts) if texts else ""


class TesseractEngine:
    """Tesseract through its command line: no Python package, but a separate Windows install."""

    name = "tesseract"

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
        """The first line of ``tesseract --version``."""
        self.load()
        done = subprocess.run(  # noqa: S603 - a fixed program and argument list, no shell
            [str(self.executable), "--version"], capture_output=True, text=True, check=True
        )
        return (done.stdout or done.stderr).splitlines()[0]

    def recognise(self, image: Path) -> str:
        if self.executable is None:
            msg = "call load() first"
            raise RuntimeError(msg)
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

    def extract(self, path: Path) -> PdfText:
        _need("pypdfium2", "pdf")
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

    def extract(self, path: Path) -> PdfText:
        _need("pdfplumber", "pdf")
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


def pdf_pages_for_corpus(path: Path) -> list[str]:
    """Page texts for the embedding spike's corpus reader (text PDFs only)."""
    return Pypdfium2Extractor().extract(path).pages
