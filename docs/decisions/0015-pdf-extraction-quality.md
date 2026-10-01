# ADR 0015: PDF extraction quality

- Status: Proposed
- Date: 2026-10-01
- Hardware-dependent: yes
- Plan reference: docs/architecture.md, section 3 (document extraction) and section 21 (Licences)

## Context

Citations depend on accurate page text with character positions, and chunking depends on heading structure. The licence side of the PDF library choice is settled (ADR 0012). Whether pypdfium2 plus pdfplumber extract well enough, and fast enough, on real documents is an empirical question tied to the owner's sample PDFs and PC.

## Decision

Proposed: extract PDFs with pypdfium2 (page text with character positions, page rendering for OCR) and use pdfplumber for font sizes in heading detection. DOCX, PPTX and XLSX use python-docx, python-pptx and openpyxl. Parsing runs in a child process with a timeout (ADR 0005).

This stays Proposed until the PDF spike has run.

## Evidence needed

- The PDF spike (issue #8) on the public fixtures (multi-column, encrypted, corrupt, rotated scan, non-English) and on the owner's sample PDFs: pages per second, text accuracy against reference text, whether heading detection works, and failure behaviour on bad files.
- Reported as metrics only, never document text.
- If the proposal does not hold up, the results and a recommended alternative go to the owner before anything changes. Docling is a candidate for hard PDFs later but pulls in PyTorch and is slower.

## Alternatives considered

- **PyMuPDF**: fastest and richest, but AGPL-3.0 (ADR 0012).
- **Docling** (IBM): excellent structure for all formats, but heavy and slower.
- **unstructured.io**: heavy.

## Consequences

- Two libraries instead of one, and pdfplumber's layout analysis is slower, so it only runs where heading detection needs it.
- Extractors are versioned, so changing one re-runs only the dependent stages.
