"""Reference texts for the OCR and PDF spikes (``scripts/spikes``), built from the ground truth.

The spikes score a reading against a text file of the same name: ``NAME.txt`` for an image (OCR
character error rate) or a text PDF (word error rate), and ``NAME.headings.txt`` for a text PDF
(heading precision and recall). They are written under ``demo/corpus/references/``, at the same
relative path as the file they describe (``receipts/coffee-receipt.jpg`` has
``references/receipts/coffee-receipt.txt``), so the spikes find them with ``--references`` and
they are not documents of the corpus: the manifest does not list them and no eval question cites
them.

What a reference holds, so that the error rates measure recognition and not layout:

- an image: every piece of text the renderer drew, one per line, in the order the renderer drew
  it. That is the order a person reads the picture: top to bottom and left to right on a line
  (a receipt's item, then its price), and the kanban board column by column, the one layout
  whose drawing order is not its row order. It holds what is drawn and nothing else: the
  ``Terminal`` label of the title bar is in it, the tab that separates a receipt's price is not.
- a text PDF: the text of each page as drawn (a list item with its ``- ``), pages in order. The
  renderer wraps a paragraph into several lines and the spikes collapse every run of whitespace
  before they compare, so the reference keeps one block per line.
- a text PDF's headings: every ``h1`` and ``h2`` block, one per line, in page order, whatever
  the spike's font-size rule would find. Each is drawn on one line, so a heading is never split.

Scanned PDFs have no reference: the OCR spike reads image files only, and the PDF spike's
``pages_without_text`` already shows that a page with no text layer gives no text; a reference
would only add a word error rate of 1.
"""

from __future__ import annotations

from model import Document
from render import image_layout

FOLDER = "references"
TEXT_SUFFIX = ".txt"
HEADINGS_SUFFIX = ".headings.txt"
IMAGE_KINDS = ("png", "jpg")


def _stem_path(doc: Document) -> str:
    """``doc.path`` without its suffix, under the references folder."""
    stem, dot, _ = doc.path.rpartition(".")
    if not dot or "/" in doc.path[len(stem) :]:
        raise ValueError(f"{doc.path}: a document needs a suffix")
    return f"{FOLDER}/{stem}"


def image_reference(doc: Document) -> str:
    """The text drawn into a screenshot or receipt, one piece per line, in drawing order."""
    return "".join(f"{placed.text}\n" for placed in image_layout(doc).placed)


def pdf_reference(doc: Document) -> str:
    """The text of every page of a text PDF, as drawn, one block per line."""
    return "".join(f"{page}\n" for page in doc.page_texts())


def pdf_headings(doc: Document) -> str:
    """The headings of a text PDF, one per line, in page order (empty when it has none)."""
    return "".join(
        f"{block.text}\n" for page in doc.blocks for block in page if block.style in ("h1", "h2")
    )


def reference_files(doc: Document) -> dict[str, str]:
    """The reference files of one document, as {path relative to demo/corpus: text}."""
    if doc.kind in IMAGE_KINDS:
        return {f"{_stem_path(doc)}{TEXT_SUFFIX}": image_reference(doc)}
    if doc.kind == "pdf_text":
        stem = _stem_path(doc)
        return {
            f"{stem}{TEXT_SUFFIX}": pdf_reference(doc),
            f"{stem}{HEADINGS_SUFFIX}": pdf_headings(doc),
        }
    return {}
