# Evaluation sets

| Set | Location | Runs |
| --- | --- | --- |
| Public, synthetic | `eval/public/questions.yaml` | In CI and by anyone, against `demo/corpus/` |
| Private, your own documents | Outside this repository, in the vault folder (for example `D:\AIVault\eval\questions.yaml`) | On your machine only; `eval/private/` is blocked by the repository safety check |

Never commit private questions: they describe personal documents.

## Public set format

`questions.yaml` is generated from `demo/generate/questions.py` by `demo/generate/build.py`. Do not
edit it by hand. Each entry looks like this:

```yaml
- id: q029
  question: How many concurrent users must the system support?
  category: version
  answerable: true
  answer: 500 (version 2). Version 1 and the project journal mention 200.
  sources:
    - path: project/requirements-v2.docx   # relative to demo/corpus/
      page: null                           # 1-based for PDFs, null for formats without pages
      quote: "NFR-2: The system shall support 500 concurrent users."
  distractors:                             # look relevant but are outdated or wrong
    - project/requirements-v1.docx
    - notes/project-journal.md
```

| Field | Meaning |
| --- | --- |
| `category` | `lookup`, `multi_doc`, `version`, `ocr`, `numeric`, `paraphrase` or `not_in_vault` |
| `answerable` | `false` for `not_in_vault`: nothing in the corpus answers it, and the right behaviour is to say so |
| `sources` | The evidence the answer needs, all of it. `quote` is verbatim text of that page, so a retrieved passage can be matched against it. A `multi_doc` question needs at least two documents, and no single document may hold all its quotes; every other answerable question needs exactly one. Every number in the answer must appear in the sources |
| `alternatives` | Passages in other documents that give the same answer. Citing one of them is as good as citing a source, so each must give the whole expected answer on its own (every number, time and date); none is required. Only a question with exactly one source has alternatives, because an alternative stands for the one required passage and cannot stand for one of several (the spike scripts refuse the file otherwise) |
| `find_document` | `true` on a "find the document" question (7 in the public set): the answer names the document that holds the evidence, and the question is also scored by Hit@1, whether the top-ranked chunk comes from that document (an exact duplicate counts as its original). It is only set on an answerable question whose sources all lie in one document, and never guessed from the category. The field is left out when false |
| `distractors` | Documents that must not be cited as the answer (for example the superseded version) |

Exact duplicates in `demo/corpus/manifest.json` (`exact_duplicate_of`) are interchangeable: a
citation of `courses/cse485/syllabus-copy.pdf` is as good as one of `syllabus.pdf`.

Ground-truth text is the text the generator drew into each file. Bullet marks are not part of a
quote for DOCX; PDFs draw a `- ` before list items. Tabs in receipts are single spaces for matching.

## Running it

From `backend/`, `uv run python -m vault.eval` imports `demo/corpus/` into a throwaway vault with
the product's own importer, extractors and chunker, asks every question through keyword search
(the code behind `POST /api/search`), and prints Recall@10, MRR and document Hit@1 by the
definitions above, with counts of what could not be indexed or scored. `--json <file>` also writes
the numbers to a new file. CI runs it on every push and fails only if it cannot run; there is no
score threshold yet. Questions whose evidence is only in a type without an extractor (PDFs and
images, for now) or one the importer refuses (CSV) are counted apart, not scored as misses.
Abstention correctness needs answers and arrives with them. The validation test in
`demo/generate/tests/` proves every source exists.

For your private set: `uv run python -m vault.eval --corpus <folder> --questions <file>`, with a
`manifest.json` in the folder (the spikes' `make-manifest` writes one; see
`scripts/spikes/README.md`). It prints numbers only, never a question, a path or document text.
Until the extraction stage runs extractors in the sandbox (it is not merged yet), the evaluation
runs them in its own process, so use it on files you would open yourself. The details are in
`backend/README.md` (Retrieval evaluation).
