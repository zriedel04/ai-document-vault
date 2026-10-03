"""Command line: ``python -m vault_spikes <check|vec|fetch-model|embedding|ocr|pdf|merge>``.

Run from ``scripts/spikes`` on the owner's Windows PC. Console output and reports hold numbers
only, never document text or file names.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from vault_spikes import adapters, spike_embedding, spike_ocr, spike_pdf, spike_vec
from vault_spikes.corpus import (
    SpikeInputError,
    build_corpus,
    load_manifest,
    load_questions,
    overlap_for,
)
from vault_spikes.errors import OwnMessage, OwnValueError
from vault_spikes.report import (
    Report,
    SpikeSchema,
    SpikeText,
    merge,
    require_free,
    with_gpu,
    with_package,
)
from vault_spikes.spike_embedding import EmbeddingInputs, run_embedding_spike
from vault_spikes.spike_ocr import find_labelled_images, run_ocr_spike
from vault_spikes.spike_pdf import run_pdf_spike
from vault_spikes.spike_vec import SqliteVecStore, probe_extension_loading, run_vec_spike

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = Path("spike-results")
OPTIONAL = {
    "sqlite_vec": "vec",
    "sentence_transformers": "embedding",
    "torch": "embedding",
    "rapidocr": "ocr",
    "onnxruntime": "ocr",
    "pypdfium2": "pdf",
    "pdfplumber": "pdf",
}


KNOWN = {
    name: SpikeSchema(
        SpikeText(module.TITLE, module.QUESTION, module.METHOD),
        module.CANDIDATES,
        module.METRICS,
        frozenset(getattr(module, "REPORT_PACKAGES", module.PACKAGES)),
    )
    for name, module in (
        ("embedding", spike_embedding),
        ("ocr", spike_ocr),
        ("pdf", spike_pdf),
        ("vec", spike_vec),
    )
}


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="vault_spikes", description=__doc__)
    commands = main.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="list which optional libraries and programs are available")

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--out", type=Path, default=DEFAULT_OUT, help="folder for the report")

    vec = commands.add_parser("vec", help="sqlite-vec loading and search latency")
    common(vec)
    vec.add_argument("--sizes", type=int, nargs="+", default=[10_000, 100_000])
    vec.add_argument("--dim", type=int, default=1024)
    vec.add_argument("--queries", type=int, default=30)

    emb = commands.add_parser("embedding", help="embedding throughput and retrieval quality")
    common(emb)
    emb.add_argument("--candidate", choices=sorted(adapters.EMBEDDING_MODELS), required=True)
    emb.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    emb.add_argument("--corpus", type=Path, default=REPO_ROOT / "demo" / "corpus")
    emb.add_argument(
        "--questions", type=Path, default=REPO_ROOT / "eval" / "public" / "questions.yaml"
    )
    emb.add_argument("--batch-size", type=int, default=16)
    emb.add_argument("--vault-chunks", type=int, default=100_000)

    fetch = commands.add_parser(
        "fetch-model", help="download an embedding model before it is measured (not timed)"
    )
    fetch.add_argument("--candidate", choices=sorted(adapters.EMBEDDING_MODELS), required=True)

    ocr = commands.add_parser("ocr", help="OCR accuracy and speed")
    common(ocr)
    ocr.add_argument("--engine", choices=["rapidocr", "tesseract"], required=True)
    ocr.add_argument("--images", type=Path, required=True)
    ocr.add_argument("--references", type=Path, help="folder of <image name>.txt (default: images)")
    ocr.add_argument("--tesseract-path")

    pdf = commands.add_parser("pdf", help="PDF extraction speed, accuracy and failure behaviour")
    common(pdf)
    pdf.add_argument("--extractor", choices=["pypdfium2", "pypdfium2+pdfplumber"], required=True)
    pdf.add_argument("--pdfs", type=Path, required=True)
    pdf.add_argument(
        "--references",
        type=Path,
        help="folder of <pdf name>.txt (text) and <pdf name>.headings.txt (headings, one per "
        "line); default: the PDF folder",
    )

    joined = commands.add_parser("merge", help="combine the reports of one spike into one")
    common(joined)
    joined.add_argument("inputs", type=Path, nargs="+", help="report .json files")
    return main


def is_public_dataset(corpus: Path, questions: Path) -> bool:
    """True only if both are the repository's own public synthetic files, whatever they are
    called. A copy elsewhere counts as the owner's own data, which is the safe way to be wrong."""
    return (
        corpus.resolve() == (REPO_ROOT / "demo" / "corpus").resolve()
        and questions.resolve() == (REPO_ROOT / "eval" / "public" / "questions.yaml").resolve()
    )


def available() -> dict[str, bool]:
    facts = {name: importlib.util.find_spec(name) is not None for name in OPTIONAL}
    facts["tesseract_on_path"] = shutil.which("tesseract") is not None
    return facts


def require_embedding_inputs(corpus: Path, questions: Path) -> None:
    """Say plainly what is missing, instead of failing in the middle of a model download.

    The defaults point at the public synthetic corpus and eval set, which arrive in the demo
    corpus pull request; until that is merged, pass ``--corpus`` and ``--questions``.
    """
    missing = [
        what
        for what, ok in (
            (
                "the corpus manifest (--corpus FOLDER with manifest.json)",
                (corpus / "manifest.json").is_file(),
            ),
            ("the eval questions (--questions FILE)", questions.is_file()),
        )
        if not ok
    ]
    if missing:
        msg = (
            "cannot find " + " and ".join(missing) + ". The public synthetic corpus and eval set "
            "(demo/corpus and eval/public) come with the demo corpus change and are not in "
            "this checkout yet; pass your own paths to run the embedding spike"
        )
        raise SpikeInputError(msg)


def show(report: Report, written: tuple[Path, Path]) -> None:
    """Print the numbers of the report (never anything else) and the names of the files written.

    Only the two file names are printed, never the folder: ``--out`` is whatever the caller typed
    (``D:\\Clients\\Patient-Name`` is a possible value), while a file name is built by this package
    from a fixed stem that ``Report.write`` checks.
    """
    for row in report.rows:
        print(row.candidate)
        for name, value in row.metrics.items():
            print(f"  {name}: {value}")
    print(f"report: {written[0].name} and {written[1].name}, in the --out folder")


def run(args: argparse.Namespace) -> int:
    if args.command == "check":
        for name, ok in available().items():
            extra = OPTIONAL.get(name)
            hint = "" if ok or not extra else f"   (uv sync --extra {extra})"
            print(f"{name}: {'yes' if ok else 'no'}{hint}")
        loading = probe_extension_loading()
        print(f"sqlite can load extensions: {'yes' if loading['can_load_extensions'] else 'NO'}")
        print(f"sqlite-vec loads: {'yes' if loading['vec_loads'] else 'no'}")
        return 0

    if args.command == "vec":
        require_free(args.out, "vec")
        with tempfile.TemporaryDirectory() as folder:
            report = run_vec_spike(
                lambda dim: SqliteVecStore(dim, Path(folder)),
                loading=probe_extension_loading(),
                sizes=tuple(args.sizes),
                dim=args.dim,
                queries=args.queries,
            )
        show(report, report.write(args.out, stem="vec"))
        return 0

    if args.command == "fetch-model":
        adapters.fetch_model(args.candidate)
        print(f"{args.candidate}: downloaded")
        return 0

    if args.command == "embedding":
        require_embedding_inputs(args.corpus, args.questions)
        embedder = adapters.SentenceTransformerEmbedder(args.candidate, args.device)
        # The same candidate is measured on the public and on the private questions, and both
        # reports are needed, so the data set is part of the name; checked before the long run.
        public = is_public_dataset(args.corpus, args.questions)
        stem = f"embedding-{embedder.name}-{'public' if public else 'private'}"
        require_free(args.out, stem)
        pdf_pages = adapters.pdf_pages_for_corpus if available()["pypdfium2"] else None
        documents = load_manifest(args.corpus / "manifest.json")
        questions = load_questions(args.questions)
        overlap = overlap_for(questions)
        inputs = EmbeddingInputs(
            build_corpus(args.corpus, documents, pdf_pages, overlap=overlap),
            documents,
            questions,
            overlap,
        )
        report = run_embedding_spike(
            embedder,
            inputs,
            batch_size=args.batch_size,
            vault_chunks=args.vault_chunks,
            public_dataset=public,
        )
        report.environment = with_gpu(report.environment, adapters.gpu_name())
        show(report, report.write(args.out, stem=stem))
        return 0

    if args.command == "ocr":
        engine: adapters.RapidOcrEngine | adapters.TesseractEngine = (
            adapters.RapidOcrEngine()
            if args.engine == "rapidocr"
            else adapters.TesseractEngine(args.tesseract_path)
        )
        require_free(args.out, f"ocr-{engine.name}")
        report = run_ocr_spike(engine, find_labelled_images(args.images, args.references))
        if isinstance(engine, adapters.TesseractEngine):
            # Rebuilt through the constructor, so the version is checked like any other.
            report.environment = with_package(report.environment, "tesseract", engine.version())
        show(report, report.write(args.out, stem=f"ocr-{engine.name}"))
        return 0

    if args.command == "pdf":
        extractor: adapters.Pypdfium2Extractor | adapters.PypdfiumPlumberExtractor = (
            adapters.Pypdfium2Extractor()
            if args.extractor == "pypdfium2"
            else adapters.PypdfiumPlumberExtractor()
        )
        require_free(args.out, f"pdf-{extractor.name.replace('+', '-')}")
        pdfs = sorted(p for p in args.pdfs.iterdir() if p.suffix.lower() == ".pdf" and p.is_file())
        report = run_pdf_spike(extractor, pdfs, references_dir=args.references)
        show(report, report.write(args.out, stem=f"pdf-{extractor.name.replace('+', '-')}"))
        return 0

    reports = [
        Report.from_json(json.loads(path.read_text(encoding="utf-8")), KNOWN)
        for path in args.inputs
    ]
    combined = merge(reports)
    # A merged report is derived, so a new merge may replace the last one, but never an input.
    target = (args.out / f"{combined.spike}.json").resolve()
    if any(path.resolve() == target for path in args.inputs):
        msg = "the merged report would overwrite one of the reports it was made from"
        raise OwnValueError(msg)
    show(combined, combined.write(args.out, overwrite=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except OwnMessage as error:
        # Fixed text written in this package; it names no file and quotes no document.
        print(f"error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # a library's error or a failed read of an input folder
        # Python's own message and traceback would carry the path of a private folder or file, or
        # text of a document, so only the kind of error is printed. Re-run the unit tests or use
        # the public dataset to investigate a failure with a full traceback.
        print(
            f"error: the run stopped with {type(error).__name__}; no further detail is printed "
            "because it could contain a file name or document text",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
