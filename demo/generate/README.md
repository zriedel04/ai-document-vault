# Demo corpus generator

Builds the synthetic documents in `demo/corpus/` and the public eval set in
`eval/public/questions.yaml`. Everything is fictional: a student called Alex Rivera, a capstone
course (CSE 485) at a made-up university, and a study-group app called StudyBuddy. All email
addresses end in `.example`. No real person, document or data was used.

The generated files are committed so that tests, CI, the demo and README screenshots never need
real documents. Do not edit them by hand; edit the generator and rebuild.

## Use

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```powershell
cd demo\generate
uv sync --locked
uv run python build.py            # write demo/corpus/ and eval/public/questions.yaml
uv run python build.py --check    # fail if the committed files differ from the generator
uv run python -m unittest discover -s tests -v
```

## What the corpus covers

44 documents in every MVP format: text PDFs, scanned-style PDFs (an image only, no text layer),
DOCX (two versions of a requirements specification, a proposal and a design review), TXT, Markdown,
CSV, PNG screenshots and JPG receipts. It also holds the traps the retrieval system must handle:
exact duplicates (a PDF, a DOCX and a JPG), a near-duplicate with two edited words, two versions that
disagree on numbers, facts that exist only inside a picture, and other courses whose syllabi look
like the CSE 485 one but say different things. Twelve documents need OCR. `manifest.json` lists every
file with its format, size, SHA-256, page count and whether OCR is needed. The size follows the plan
(about 40 to 60 documents).

## How it is built

| File | Role |
| --- | --- |
| `model.py` | `Document` and `Block`: the ground-truth text of each document |
| `content_courses.py`, `content_project.py`, `content_media.py`, `content_extra.py` | The fictional content |
| `render.py` | Turns a `Document` into bytes (PDF, scanned PDF, DOCX, PNG, JPG) |
| `questions.py` | The eval questions, each with sources and verbatim quotes |
| `build.py` | Writes or checks the corpus, manifest and eval set |
| `tests/test_corpus.py` | Validation: every cited document, page and quote exists; no drawn text overlaps or runs off its picture |

The content is written once as ground truth. The renderer draws it, so a quote in an eval question
can be checked against the same text that was drawn into the PDF or picture.

## Determinism

Running the build twice gives identical bytes:

- no timestamps: reportlab runs with `invariant=1`, DOCX zip entries have a fixed date, PDF and DOCX
  metadata are fixed;
- the noise in scans and receipts comes from `random.Random(seed)` with a seed per document;
- fonts are the ones bundled with the pinned libraries (Pillow's default font and the PDF standard
  Helvetica), so no font files are committed and none need a separate licence;
- library versions are pinned by `uv.lock`.

Different library builds can produce different compressed bytes, so the byte-for-byte comparison
(`build.py --check`) runs on Linux in CI. The tests run on Windows too and check that the corpus is
valid and deterministic on that machine.

## Licences

The generator depends on Pillow (MIT-CMU, a permissive licence; listed as a reviewed exception in
`scripts/check_python_licenses.py`) and reportlab (BSD). The generator is a development tool and is
not shipped with the application. The committed content is original and may be reused under the
project licence.
