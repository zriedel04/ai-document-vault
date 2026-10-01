# Phase 0 spike scripts

Measurement scripts for the hardware-dependent decisions in [ADR 0004](../../docs/decisions/0004-sqlite-instead-of-postgresql.md) (sqlite-vec), [0013](../../docs/decisions/0013-embedding-model.md) (embedding model), [0014](../../docs/decisions/0014-ocr-engine.md) (OCR engine) and [0015](../../docs/decisions/0015-pdf-extraction-quality.md) (PDF extraction). **They are run on the owner's Windows PC, never on another machine as a substitute.** CI runs only the unit tests, which use fakes and measure nothing.

Nothing here has been benchmarked yet. The numbers will come from the owner's PC once its specifications are shared.

## What the scripts record

Metrics only: counts, rates, timings, scores. A report cannot hold document text: `Report.add` accepts only numbers and booleans, and names are checked against strict patterns. Console output is built from the same report, so extracted and OCR text, file names and paths are never printed or stored. Reports are written to `spike-results/` (git-ignored) and the owner decides which numbers go into `docs/spikes/NN-topic.md`.

## Set up (Windows PowerShell, Python 3.12 and uv)

```powershell
cd scripts\spikes
uv sync --extra vec --extra pdf          # add --extra embedding and --extra ocr for those spikes
uv run python -m vault_spikes check      # what is installed, and whether sqlite-vec loads
```

There is no lock file in the repository: the versions that were actually installed are recorded in each report's Environment section. The heavy libraries are extras so that CI never installs them.

## Commands

| Spike | Command | Feeds |
| --- | --- | --- |
| sqlite-vec | `uv run python -m vault_spikes vec` | ADR 0004 |
| Embedding model | `uv run python -m vault_spikes embedding --candidate qwen3-embedding-0.6b --device cpu` (then `--device cuda`, and `embeddinggemma-300m`) | ADR 0013 |
| OCR | `uv run python -m vault_spikes ocr --engine rapidocr --images <folder>` (then `--engine tesseract`) | ADR 0014 |
| PDF | `uv run python -m vault_spikes pdf --extractor pypdfium2 --pdfs <folder>` (then `pypdfium2+pdfplumber`) | ADR 0015 |
| Combine | `uv run python -m vault_spikes merge spike-results\embedding-*.json` | one table per spike |

Each process measures one candidate, so memory numbers do not mix, and `merge` joins the reports. OCR images and PDFs may have a reference text file of the same name (`scan1.png` and `scan1.txt`); the script reports the error rate and discards both texts. The embedding spike runs on the public synthetic corpus in `demo/corpus/` and the public eval set in `eval/public/`; point `--corpus` and `--questions` elsewhere to run on private files, which must stay outside the repository.

## From a report to a spike file

Each report is written as JSON and as a Markdown draft with the five sections `scripts/check_adrs.py` requires. The Conclusion is a placeholder on purpose. The owner reviews the numbers, copies the draft to `docs/spikes/NN-topic.md`, writes the conclusion and only then links it from the ADR. See [docs/spikes/README.md](../../docs/spikes/README.md).

## What these scripts cannot show

The unit tests prove that the measurements are computed and recorded correctly with fakes. They do not prove that the sentence-transformers, RapidOCR and Tesseract adapters run on the PC: their first real run is the first test. If an adapter fails there, that is a bug in the script, not a result about the technology.

## Checks

```
cd scripts/spikes
uv run --with pyyaml python -m unittest discover -s tests -v
uvx ruff check . ; uvx ruff format --check .
uvx --with pyyaml --with types-pyyaml mypy
```
