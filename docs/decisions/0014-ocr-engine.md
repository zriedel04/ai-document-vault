# ADR 0014: OCR engine

- Status: Proposed
- Date: 2026-10-01
- Hardware-dependent: yes
- Plan reference: docs/architecture.md, section 3 (OCR) and section 22 (decision 8)

## Context

Screenshots, receipts and scanned PDFs need local OCR. Quality varies a lot between engines on screenshots and mixed layouts, and speed depends on the Windows PC. OCR quality is the biggest unknown in the plan.

## Decision

Proposed: RapidOCR with PP-OCRv5 models on ONNX Runtime (installs as a Python package, no system binary), compared against Tesseract 5.5 before it is adopted. OCR is decided per page, not per file: a PDF page is OCRed only if its text layer is empty or obviously garbage. Results are cached by file hash plus OCR engine and model version.

This stays Proposed until the OCR spike has run on the Windows PC.

## Evidence needed

- The OCR spike (issue #8) on the Windows PC, on about 20 of the owner's screenshots and scans plus the synthetic corpus: character error rate against reference text, pages per minute, memory use and install friction on Windows.
- Reported as metrics only. Neither the owner's images nor the recognised text is committed.
- If the proposal does not hold up, the results and a recommended alternative go to the owner before anything changes.

## Alternatives considered

- **Tesseract 5.5**: very fast, but needs a separate Windows install and is weaker on screenshots.
- **Full PaddleOCR**: most accurate, but a heavy install.
- **Surya**: strong layouts, but GPL-3.0 (ADR 0012).
- **Windows built-in OCR**: no install, fewer controls.

## Consequences

- The OCR stage must sit behind an interface so the engine can change without touching the pipeline.
- OCR is the most expensive local stage (CPU), so it runs with a concurrency limit of 1 (ADR 0005).
