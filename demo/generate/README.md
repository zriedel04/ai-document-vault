# Demo corpus generator

Builds the synthetic documents in `demo/corpus/`, the reference texts for the OCR and PDF spikes
in `demo/corpus/references/` and the public eval set in `eval/public/questions.yaml`. Everything is fictional: a student called Alex Rivera, a capstone
course (CSE 485) at a made-up university, and a study-group app called StudyBuddy. All email
addresses end in `.example`. No real person, document or data was used.

The generated files are committed so that tests, CI, the demo and README screenshots never need
real documents. Do not edit them by hand; edit the generator and rebuild.

## Use

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```powershell
cd demo\generate
uv sync --locked
uv run python build.py            # write demo/corpus/ (with references/) and eval/public/questions.yaml
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
| `references.py` | The reference texts for the OCR and PDF spikes, from the same ground truth |
| `build.py` | Writes or checks the corpus, manifest and eval set |
| `tests/test_corpus.py` | Validation: every cited document, page and quote exists; no drawn text overlaps or runs off its picture; every reference text matches what was drawn |

The content is written once as ground truth. The renderer draws it, so a quote in an eval question
can be checked against the same text that was drawn into the PDF or picture.

## Reference texts for the spikes

The OCR and PDF spikes in `scripts/spikes/` score a reading against a text file of the same name.
`references.py` writes those files to `demo/corpus/references/`, at the same path as the file
each describes, from the content the renderer draws (never by running OCR or copying by hand):

| Corpus file | Reference files | What they hold |
| --- | --- | --- |
| Screenshot or receipt (`receipts/coffee-receipt.jpg`) | `references/receipts/coffee-receipt.txt` | Every piece of text drawn into the picture, one per line, in reading order |
| Text PDF (`courses/cse485/syllabus.pdf`) | `references/courses/cse485/syllabus.txt` | The text of every page as drawn, one paragraph, heading or list item (with its `- `) per line |
| | `references/courses/cse485/syllabus.headings.txt` | Every heading (`h1` and `h2`), one per line, in page order |
| Scanned PDF, DOCX, TXT, Markdown, CSV | none | |

So that an error rate measures recognition and not layout:

- **Whitespace does not count.** The spikes collapse every run of spaces and line breaks before
  they compare, so where a line breaks does not matter: a paragraph the PDF wraps over three lines
  is one line of its reference, and a receipt's item and price are two lines.
- **Order counts.** A reference follows the picture from top to bottom and from left to right on a
  line (a receipt's item, then its price), the order OCR output comes in. The kanban board is the
  one exception: its reference reads the columns one after the other, as a person reads a board,
  so an engine that reads it row by row is scored down for its reading order on that image.
- **Only what is drawn.** The terminal screenshot's title bar label (`Terminal`) is in its
  reference; the tab that separates a receipt's price in the content is not, and nothing that the
  photo effects (rotation, blur, noise) change is described.
- **Headings are all the headings,** not what the PDF spike's font-size rule would find. The `h2`
  headings are 13 pt beside 11 pt text (1.18 times), below the rule's 1.2, so heading recall on the
  public data measures that rule as it is.
- **Scanned PDFs have none.** The OCR spike reads image files only, and the PDF spike counts a page
  without a text layer in `pages_without_text`; a reference would only add a word error rate of 1.

The reference files are not documents: the manifest does not list them and no eval question cites
them, so the embedding spike (which reads the manifest) and the eval set never see them, and the
demo import is to take the manifest's documents, not every file under `demo/corpus/`. The tests
check each reference against an account that does not go through `references.py`: the words of the
content and the layout's drawn boxes for a picture, and the strings in the PDF's own bytes (the
bold ones for the headings) for a PDF.

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
