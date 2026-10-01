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
| `sources` | The evidence the answer needs, all of it. `quote` is verbatim text of that page, so a retrieved passage can be matched against it. A `multi_doc` question needs at least two documents; every other answerable question needs exactly one |
| `alternatives` | Passages in other documents that give the same answer. Citing one of them is as good as citing a source; none is required |
| `distractors` | Documents that must not be cited as the answer (for example the superseded version) |

Exact duplicates in `demo/corpus/manifest.json` (`exact_duplicate_of`) are interchangeable: a
citation of `courses/cse485/syllabus-copy.pdf` is as good as one of `syllabus.pdf`.

Ground-truth text is the text the generator drew into each file. Bullet marks are not part of a
quote for DOCX; PDFs draw a `- ` before list items. Tabs in receipts are single spaces for matching.

The metrics that use this set (Recall@10, MRR, document Hit@1, abstention correctness) arrive with
the retrieval phases; the validation test in `demo/generate/tests/` already proves every source
exists.
