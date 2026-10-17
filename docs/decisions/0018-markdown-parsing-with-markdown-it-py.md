# ADR 0018: Markdown parsing with markdown-it-py

- Status: Accepted
- Date: 2026-10-17
- Hardware-dependent: no
- Plan reference: docs/architecture.md, section 3 (stack table, document extraction)

## Context

The plan's stack table said "Markdown/TXT: direct": the text extractor (`vault.ingest.extractors.text`) found Markdown's headings with a hand-written parser. Headings are where chunks split and what citations name, so a heading the parser misses or invents moves a section boundary.

CommonMark's heading rules depend on every container rule around them: block quotes, list items and their content columns, lazy continuation lines, fenced and indented code, link reference definitions. In the review of PR #46, five Codex passes each found another container case the hand parser got wrong (lazy lines, a fence in a nested list item, a fence in a block quote, and others), and the fix for each pass was a new branch in an already large state machine. A sixth case (fenced code inside a block quote taking a lazy line) was found while that fix was being written.

markdown-it-py (MIT, depending only on mdurl, MIT) is a maintained port of markdown-it, with a `commonmark` preset that follows the specification and ships type information. Its top-level headings were compared with those of the CommonMark reference implementation (commonmark.py, a port of commonmark.js) on about 200,000 generated documents. They were identical except on documents built around link reference definitions (about 0.3% of the generated documents, all containing lines such as `[name]: /url`), where markdown-it-py departs from the specification: it takes a definition out of a paragraph as soon as it reads it, while the specification takes definitions out when the paragraph closes. So `[c]:` followed by `===` is a level-1 heading by the specification and a definition with the destination `===` for markdown-it-py, and `[ref]: /url`, `-`, `---` is a level-2 heading `-` by the specification and a definition, an empty list item and a thematic break for markdown-it-py.

## Decision

- Markdown headings are found with markdown-it-py's `commonmark` preset. Only its token stream is used; nothing is rendered.
- A heading is recorded only at the top level (token nesting level 0). Headings inside block quotes and list items are not section headings of the document.
- A heading's offsets run from the start of its first source line (for a setext heading under link reference definitions, the first line after them) to the end of its last line, without the line break, from the token's line map. The title is the inline token's raw source: an ATX title without its closing sequence, a setext title with its lines stripped and joined by single spaces.
- YAML front matter at the very start is still skipped by the extractor before parsing, as before.
- The known deviations on link reference definitions are accepted and recorded as tests (`test_known_deviations_from_commonmark_on_link_reference_definitions`), so an upgrade that changes them is noticed.
- This replaces "Markdown/TXT: direct" in the plan's stack table for Markdown; the table now names markdown-it-py and this ADR.

## Approval

- Approved by the owner on 2026-10-17, choosing markdown-it-py over keeping the hand-written parser after the comparison above. The code and its tests are in PR #46.

## Alternatives considered

- **Keep the hand-written parser**: each review pass found another container case, and nothing suggested the list was nearly exhausted. More review rounds for a format a library already implements.
- **commonmark.py**: the reference implementation and exact on the cases above, but unmaintained since 2019.
- **mistune**: fast and maintained, but not CommonMark-compliant, so it would bring its own heading differences.

## Consequences

- The container rules, HTML blocks and code blocks are the library's, so the two simplifications the hand parser documented (HTML blocks not recognised, fenced code in list items read as paragraph text) are gone.
- One more runtime dependency (two packages, both MIT), checked by the licence check like every other.
- On documents with link reference definitions next to a setext underline, a heading can differ from the specification. That changes only where a section starts. If markdown-it-py fixes this, the deviation tests fail and are updated with the upgrade.
- The extractor version (`text-1`) is unchanged, since nothing has been extracted with the old parser outside tests: the extractor is not wired into the job queue yet.
