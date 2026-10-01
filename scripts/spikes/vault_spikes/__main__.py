"""Command line: ``python -m vault_spikes <check|vec|embedding|ocr|pdf|merge>``.

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

from vault_spikes import adapters
from vault_spikes.corpus import SpikeInputError, build_corpus, load_manifest, load_questions
from vault_spikes.report import Report, merge
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
    pdf.add_argument("--references", type=Path, help="folder of <pdf name>.txt (default: pdfs)")

    joined = commands.add_parser("merge", help="combine the reports of one spike into one")
    common(joined)
    joined.add_argument("inputs", type=Path, nargs="+", help="report .json files")
    return main


def available() -> dict[str, bool]:
    facts = {name: importlib.util.find_spec(name) is not None for name in OPTIONAL}
    facts["tesseract_on_path"] = shutil.which("tesseract") is not None
    return facts


def show(report: Report, written: tuple[Path, Path]) -> None:
    """Print the numbers of the report (never anything else) and where it was written."""
    for row in report.rows:
        print(row.candidate)
        for name, value in row.metrics.items():
            print(f"  {name}: {value}")
    print(f"report: {written[0]} and {written[1]}")


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

    if args.command == "embedding":
        embedder = adapters.SentenceTransformerEmbedder(args.candidate, args.device)
        pdf_pages = adapters.pdf_pages_for_corpus if available()["pypdfium2"] else None
        documents = load_manifest(args.corpus / "manifest.json")
        inputs = EmbeddingInputs(
            build_corpus(args.corpus, documents, pdf_pages),
            documents,
            load_questions(args.questions),
        )
        report = run_embedding_spike(
            embedder, inputs, batch_size=args.batch_size, vault_chunks=args.vault_chunks
        )
        report.environment = type(report.environment)(
            **{**report.environment.__dict__, "gpu": adapters.gpu_name()}
        )
        show(report, report.write(args.out, stem=f"embedding-{embedder.name}"))
        return 0

    if args.command == "ocr":
        engine: adapters.RapidOcrEngine | adapters.TesseractEngine = (
            adapters.RapidOcrEngine()
            if args.engine == "rapidocr"
            else adapters.TesseractEngine(args.tesseract_path)
        )
        report = run_ocr_spike(engine, find_labelled_images(args.images, args.references))
        if isinstance(engine, adapters.TesseractEngine):
            report.environment.packages["tesseract"] = engine.version()
        show(report, report.write(args.out, stem=f"ocr-{engine.name}"))
        return 0

    if args.command == "pdf":
        extractor: adapters.Pypdfium2Extractor | adapters.PypdfiumPlumberExtractor = (
            adapters.Pypdfium2Extractor()
            if args.extractor == "pypdfium2"
            else adapters.PypdfiumPlumberExtractor()
        )
        pdfs = sorted(p for p in args.pdfs.iterdir() if p.suffix.lower() == ".pdf" and p.is_file())
        report = run_pdf_spike(extractor, pdfs, references_dir=args.references)
        show(report, report.write(args.out, stem=f"pdf-{extractor.name.replace('+', '-')}"))
        return 0

    reports = [
        Report.from_json(json.loads(path.read_text(encoding="utf-8"))) for path in args.inputs
    ]
    combined = merge(reports)
    show(combined, combined.write(args.out))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (adapters.MissingDependencyError, SpikeInputError, ValueError) as error:
        # These messages are written in this package and never quote document text.
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
