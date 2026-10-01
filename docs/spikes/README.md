# Spikes

A spike is a short, time-boxed experiment that settles a hardware-dependent or risky technology choice. The spikes for Phase 0 run on the owner's Windows PC only (never on another machine as a substitute), after the owner has shared the PC's specifications. They decide the Proposed ADRs: [embedding model](../decisions/0013-embedding-model.md), [OCR engine](../decisions/0014-ocr-engine.md) and [PDF extraction quality](../decisions/0015-pdf-extraction-quality.md).

## What is recorded

Metrics and conclusions only. **Never document text.** That means never the text of a private document, extracted or OCR text, file names or paths that reveal private content, screenshots of documents, or embeddings of them. Results measured on private sample files are reported as numbers (counts, rates, timings, scores), and results on the public synthetic corpus can name the synthetic files.

## One file per spike

Name the file `NN-topic.md` (for example `01-embedding-model.md`) and include:

- **Question**: what the spike decides, and the ADR it feeds.
- **Environment**: Windows version, CPU, RAM, GPU and VRAM, Python version, package versions.
- **Method**: the script and the commands (scripts live in `scripts/spikes/`), the data set (synthetic, private sample, or both) and how it was measured.
- **Results**: a table of metrics, per candidate.
- **Conclusion**: which candidate wins and why, or a recommended alternative.

## From results to a decision

1. Bring the results to the owner. If an assumption did not hold, include a recommended alternative. Do not change direction before then.
2. Update the ADR: link the spike (`- Spike results: docs/spikes/NN-topic.md`), then set it to Accepted, or add a new ADR that supersedes it.
