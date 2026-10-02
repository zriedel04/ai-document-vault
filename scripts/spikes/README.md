# Phase 0 spike scripts

Measurement scripts for the hardware-dependent decisions in [ADR 0004](../../docs/decisions/0004-sqlite-instead-of-postgresql.md) (sqlite-vec), [0013](../../docs/decisions/0013-embedding-model.md) (embedding model), [0014](../../docs/decisions/0014-ocr-engine.md) (OCR engine) and [0015](../../docs/decisions/0015-pdf-extraction-quality.md) (PDF extraction). **They are run on the owner's Windows PC, never on another machine as a substitute.** CI runs only the unit tests, which use fakes and measure nothing.

Nothing here has been benchmarked yet. The numbers will come from the owner's PC once its specifications are shared.

## What the scripts record

Metrics only: counts, rates, timings, scores. A report cannot hold document text: `Report.add` accepts only numbers and booleans, and names are checked against strict patterns. Console output is built from the same report, so extracted and OCR text, file names and paths are never printed or stored. Reports are written to `spike-results/` (git-ignored) and the owner decides which numbers go into `docs/spikes/NN-topic.md`.

## Set up (Windows PowerShell)

Install [uv](https://docs.astral.sh/uv/) if `uv --version` is not found (`winget install --id astral-sh.uv`), then open a new terminal. The scripts need Python 3.12; uv downloads it by itself, so a newer Python on the PC (3.13 or 3.14) is not used and does not need to be removed.

```powershell
cd scripts\spikes
uv sync --python 3.12 --extra vec --extra pdf   # add --extra embedding and --extra ocr for those spikes
uv run python -m vault_spikes check             # what is installed, and whether sqlite-vec loads
```

There is no lock file in the repository: the versions that were actually installed are recorded in each report's Environment section. The heavy libraries are extras so that CI never installs them.

**GPU for the embedding spike.** The `torch` that PyPI serves for Windows is CPU-only, so `--device cuda` stops with a message until the CUDA build is installed. Install it with the command from pytorch.org (Get Started, Windows, Pip, CUDA), then run every later command with `uv run --no-sync` so uv does not put the CPU build back. Run the `--device cpu` measurements first, because they need none of this.

## Commands

| Spike | Command | Feeds |
| --- | --- | --- |
| sqlite-vec | `uv run python -m vault_spikes vec` | ADR 0004 |
| Embedding model | first `uv run python -m vault_spikes fetch-model --candidate qwen3-embedding-0.6b` (downloads the model once, not timed), then `uv run python -m vault_spikes embedding --candidate qwen3-embedding-0.6b --device cpu` (then `--device cuda`, and the same two steps for `embeddinggemma-300m`) | ADR 0013 |
| OCR | `uv run python -m vault_spikes ocr --engine rapidocr --images <folder>` (then `--engine tesseract`) | ADR 0014 |
| PDF | `uv run python -m vault_spikes pdf --extractor pypdfium2 --pdfs <folder>` (then `pypdfium2+pdfplumber`) | ADR 0015 |
| Combine | `uv run python -m vault_spikes merge spike-results\embedding-*.json` | one table per spike |

Each process measures one candidate, so memory numbers do not mix, and `merge` joins the reports. `merge` accepts only reports from this script: a report's title, question and method must equal the spike's own text, and its machine description (system, CPU, GPU, package versions) must be a short plain description, so a hand-edited file cannot carry other text into a merged report. A machine description is still free-form text that a pattern cannot judge, so read the Environment lines of a report before committing it. OCR images and PDFs may have a reference text file of the same name (`scan1.png` and `scan1.txt`); the script reports the error rate and discards both texts. A PDF may also have `name.headings.txt`, one expected heading per line: with `--extractor pypdfium2+pdfplumber` the report then gives heading precision, recall and F1 (a count of candidates alone cannot tell good detection from bad, so without such a file no heading accuracy is claimed). An empty `name.headings.txt` says the PDF has no headings, so every heading found in it counts as a false one. Retrieval in the embedding spike is scored by passage for Recall@10 and MRR: a chunk counts only if it comes from the right document and holds the `quote` of the eval file, and the report counts required passages that no chunk holds at all. Hit@1 is scored by document, as the architecture plan defines it, and only for the questions whose evidence lies in one document (so not multi-document or version questions): it asks whether the top chunk comes from a document that holds the evidence, and the report gives how many questions it covers (`hit_at_1_questions`). If no question qualifies, `hit_at_1` is left out instead of reading as 0. A document the script cannot read (an encrypted or corrupt PDF, a damaged DOCX) is skipped and counted with the scans, not a reason to stop the run. `merge` keeps each candidate's own environment: the documented order runs the CPU candidates, then swaps the CPU-only torch build for a CUDA build, so a merged report lists both environments by candidate instead of showing the first one for every row. The embedding spike defaults to the public synthetic corpus in `demo/corpus/` and the public eval set in `eval/public/`, which arrive with the demo corpus pull request (until then it stops with a message and you pass `--corpus` and `--questions`); point `--corpus` and `--questions` elsewhere to run on private files, which must stay outside the repository.

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
