"""The retrieval evaluation (plan, "Measure retrieval, don't eyeball it", and section 20): import an
eval corpus into a throwaway vault, run the product's own pipeline on it, ask every question of the
eval file through keyword search, and score the rankings.

What runs is the product's code, not a model of it:

- every file the manifest lists is imported by :class:`vault.ingest.importer.Importer`, as an
  upload of a file with its own name (the name is the document's title, which the keyword index
  searches), so validation, the blob store and exact-duplicate detection are the product's;
- each new document's blob is read by the extractor of its type and the pages are stored with
  ``vault.db.pages.replace_pages``, then the chunk stage runs (``chunk_document``). The extraction
  stage that does this from the job queue is not on ``main`` yet, so the extractors are called
  here directly and in this process, the way that stage calls them in the sandbox; once it is,
  this runs the stage instead. Until then, run it only on documents you would open yourself;
- each question's text is the query of :func:`vault.retrieval.keyword.search`, the function behind
  ``POST /api/search``, with a page of ``K`` results. Search returns one result per document (its
  best chunk), as the Search page shows, so the ranking is those chunks in that order.

The scores are the spike scripts' own (``vault_spikes.retrieval``): Recall@10 and MRR by passage,
Hit@1 by document for the questions the eval file marks ``find_document``, with exact duplicates
interchangeable and alternatives counted by ``accepted_passages``. The questions and manifest are
read by the spikes' loaders too (``vault_spikes.corpus``), so there is one definition of the eval
format and of every score. Exact duplicates are the documents the importer stored once: a mark in
the manifest that the importer does not confirm stops the run.

A question is scored only if ``can_be_scored`` says the indexed documents can answer it: a question
whose evidence is in a type that has no extractor yet (PDF, images) or that the importer refuses
(CSV) is counted apart, never scored as a miss. A query that search refuses (too long, too many
terms) is scored as a miss, since search finds nothing for it, and counted.

Everything in the result is a count, a score or a word of this package's own vocabulary (a
refusal reason, a stored type), never a question, an id, a path or document text, so the result
can be printed for a private corpus. Errors raised here carry fixed text only.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import Engine, select
from vault_spikes.corpus import (
    Chunk,
    Document,
    Question,
    check_sources_listed,
    load_manifest,
    load_questions,
    safe_file,
)
from vault_spikes.errors import OwnRuntimeError
from vault_spikes.retrieval import (
    K,
    accepted_passages,
    can_be_scored,
    score_all,
    sources_missing_from,
)

from vault.db import ids
from vault.db.engine import create_vault_engine
from vault.db.migrate import upgrade_to_latest
from vault.db.pages import PageHeading, PageText, replace_pages
from vault.db.schema import blobs, chunks, document_versions, documents
from vault.ingest import chunk_stage
from vault.ingest.chunking import structural_v1
from vault.ingest.extractors import Extraction, ExtractionError
from vault.ingest.extractors import docx as docx_extractor
from vault.ingest.extractors import text as text_extractor
from vault.ingest.importer import Importer, ImportRequest, Outcome
from vault.retrieval.keyword import QueryError, SearchQuery, search
from vault.storage.blobs import BlobStore
from vault.storage.layout import ensure_vault

SEARCH: Final = "keyword"
# The stored types (``vault.ingest.validate.MIME_TYPES``) that have an extractor on this branch.
EXTRACTED: Final = frozenset({"txt", "md", "docx"})


class EvalError(OwnRuntimeError):
    """The run cannot go on; the message is fixed text."""


@dataclass(frozen=True)
class Documents:
    """What became of each file the manifest lists. Every file is in exactly one group, so the
    groups add up to ``listed``."""

    listed: int
    indexed: int  # a new document whose pages were extracted and chunked
    duplicates: int  # the same bytes as another listed file: stored once, as that document
    refused: Mapping[str, int]  # by the importer's refusal reason
    no_extractor: Mapping[str, int]  # by stored type: imported, but nothing reads it yet
    extraction_failed: int  # the extractor refused it, or its pages broke the page rules
    chunks: int


@dataclass(frozen=True)
class Questions:
    """How the questions were used. ``scored`` + ``none_indexed`` + ``partly_indexed`` is the
    number of answerable questions; with ``not_answerable`` it is ``total``."""

    total: int
    not_answerable: int  # nothing in the corpus answers them: abstention, not retrieval
    scored: int
    none_indexed: int  # no passage that counts is in an indexed document
    partly_indexed: int  # some are, but not enough to answer (a source is not indexed)
    queries_refused: int  # scored as misses: search refuses the query
    passages_missing: int  # required passages of scored questions that no chunk holds


@dataclass(frozen=True)
class Scores:
    recall_at_10: float | None
    mrr_at_10: float | None
    hit_at_1: float | None
    hit_at_1_questions: int


@dataclass(frozen=True)
class Evaluation:
    search: str
    chunker: str
    # The code page a text file without a byte order mark that is not UTF-8 is read in: the
    # product's default, not the owner's ``[processing] text_fallback_encoding``.
    text_fallback_encoding: str
    k: int
    documents: Documents
    questions: Questions
    scores: Scores

    def as_json(self) -> dict[str, object]:
        d, q, s = self.documents, self.questions, self.scores
        return {
            "search": self.search,
            "chunker": self.chunker,
            "text_fallback_encoding": self.text_fallback_encoding,
            "k": self.k,
            "documents": {
                "listed": d.listed,
                "indexed": d.indexed,
                "duplicates": d.duplicates,
                "refused": dict(sorted(d.refused.items())),
                "no_extractor": dict(sorted(d.no_extractor.items())),
                "extraction_failed": d.extraction_failed,
                "chunks": d.chunks,
            },
            "questions": {
                "total": q.total,
                "not_answerable": q.not_answerable,
                "scored": q.scored,
                "none_indexed": q.none_indexed,
                "partly_indexed": q.partly_indexed,
                "queries_refused": q.queries_refused,
                "passages_missing": q.passages_missing,
            },
            "scores": {
                "recall_at_10": s.recall_at_10,
                "mrr_at_10": s.mrr_at_10,
                "hit_at_1": s.hit_at_1,
                "hit_at_1_questions": s.hit_at_1_questions,
            },
        }


def extract(extension: str, data: bytes) -> Extraction:
    """The extraction of a blob of a type in ``EXTRACTED``, as the extraction stage runs it."""
    if extension == "docx":
        return docx_extractor.extract(data)
    return text_extractor.extract(
        data, markdown=extension == "md", fallback=text_extractor.FALLBACK_ENCODING
    )


def evaluate(corpus: Path, questions_file: Path) -> Evaluation:
    """Run the whole evaluation in a vault made for it in a temporary folder, removed after,
    whether the run succeeds or not."""
    documents = load_manifest(corpus / "manifest.json")
    questions = load_questions(questions_file)
    check_sources_listed(questions, documents)
    files = [safe_file(corpus, doc.path) for doc in documents]  # before anything is imported
    work = tempfile.TemporaryDirectory(prefix="vault-eval-")
    try:
        paths = ensure_vault(Path(work.name) / "vault")
        upgrade_to_latest(paths.db)
        engine = create_vault_engine(paths.db)
        try:
            return _run(engine, BlobStore(paths.blobs), documents, files, questions)
        finally:
            engine.dispose()  # Windows removes no file that SQLite still holds open
    finally:
        # The vault holds the corpus's text, so a copy left behind is reported, never ignored.
        # (``cleanup`` makes the read-only blobs writable before it removes them.)
        try:
            work.cleanup()
        except OSError:
            msg = (
                "the temporary vault could not be removed; delete the vault-eval- folder in the "
                "system's temporary folder"
            )
            raise EvalError(msg) from None


def _run(
    engine: Engine,
    store: BlobStore,
    listed: Sequence[Document],
    files: Sequence[Path],
    questions: Sequence[Question],
) -> Evaluation:
    importer = Importer(engine, store)
    batch = ids.new_ulid()
    document_of: dict[str, str] = {}  # manifest path to document id
    first_path: dict[str, str] = {}  # document id to the first listed path stored as it
    new: list[str] = []
    refused: Counter[str] = Counter()
    duplicates = 0
    for doc, file in zip(listed, files, strict=True):
        result = importer.import_file(
            ImportRequest(path=file, original_filename=file.name, method="upload", batch_id=batch)
        )
        if result.outcome is Outcome.REFUSED or result.document_id is None:
            refused[result.reason.value if result.reason is not None else "unknown"] += 1
            continue
        document_of[doc.path] = result.document_id
        if result.outcome is Outcome.DUPLICATE:
            duplicates += 1
        else:
            new.append(result.document_id)
        first_path.setdefault(result.document_id, doc.path)
    _check_duplicate_marks(listed, document_of)

    indexed: set[str] = set()
    no_extractor: Counter[str] = Counter()
    failed = 0
    for document_id in new:
        version_id, sha256, extension = _current_blob(engine, document_id)
        if extension not in EXTRACTED:
            no_extractor[extension] += 1
            continue
        with store.open(sha256) as stream:
            data = stream.read()
        try:
            extraction = extract(extension, data)
            replace_pages(
                engine,
                version_id,
                [
                    PageText(
                        page.number,
                        page.text,
                        tuple(PageHeading(h.level, h.title, h.start, h.end) for h in page.headings),
                    )
                    for page in extraction.pages
                ],
                extraction.extractor,
                extraction.encoding,
            )
        except (ExtractionError, ValueError):
            failed += 1
            continue
        if (
            chunk_stage.chunk_document(engine, document_id, sha256)
            is not chunk_stage.Outcome.STORED
        ):
            msg = "the chunk stage did not store the chunks of a document it was given"
            raise EvalError(msg)
        indexed.add(document_id)

    # Every listed path stands for the first listed path the importer stored as the same document,
    # so exact duplicates are one document to the scores, as the importer made them.
    canonical = {
        doc.path: first_path.get(document_of.get(doc.path, ""), doc.path) for doc in listed
    }
    readable = frozenset(path for path, doc_id in document_of.items() if doc_id in indexed)
    every_chunk = _chunks(engine, first_path)

    answerable = [q for q in questions if q.answerable]
    scorable = [q for q in answerable if can_be_scored(q, readable)]
    none_indexed = sum(
        1
        for q in answerable
        if not can_be_scored(q, readable)
        and not any(p.path in readable for p in accepted_passages(q))
    )
    ranked: dict[str, list[Chunk]] = {}
    queries_refused = 0
    for question in scorable:
        try:
            ranked[question.id] = _ranking(engine, question.text, first_path)
        except QueryError:
            ranked[question.id] = []
            queries_refused += 1
    scores = Scores(None, None, None, 0)
    if scorable:
        computed = score_all(scorable, ranked, canonical)
        scores = Scores(
            computed.recall_at_10,
            computed.mrr_at_10,
            computed.hit_at_1,
            computed.hit_at_1_questions,
        )
    return Evaluation(
        search=SEARCH,
        chunker=structural_v1.CHUNKER_VERSION,
        text_fallback_encoding=text_extractor.FALLBACK_ENCODING,
        k=K,
        documents=Documents(
            listed=len(listed),
            indexed=len(indexed),
            duplicates=duplicates,
            refused=dict(refused),
            no_extractor=dict(no_extractor),
            extraction_failed=failed,
            chunks=len(every_chunk),
        ),
        questions=Questions(
            total=len(questions),
            not_answerable=len(questions) - len(answerable),
            scored=len(scorable),
            none_indexed=none_indexed,
            partly_indexed=len(answerable) - len(scorable) - none_indexed,
            queries_refused=queries_refused,
            passages_missing=sources_missing_from(scorable, every_chunk, canonical, readable),
        ),
        scores=scores,
    )


def _check_duplicate_marks(listed: Sequence[Document], document_of: Mapping[str, str]) -> None:
    """Stop the run if the manifest marks a file as an exact duplicate of another and the importer,
    which compares the bytes, stored the two as different documents. The scores treat the two as
    one; a mark the bytes do not support would score a question against the wrong file."""
    for doc in listed:
        if doc.duplicate_of is None:
            continue
        copy, original = document_of.get(doc.path), document_of.get(doc.duplicate_of)
        if copy is not None and original is not None and copy != original:
            msg = "a document the manifest marks as an exact duplicate was stored as another one"
            raise EvalError(msg)


def _current_blob(engine: Engine, document_id: str) -> tuple[str, str, str]:
    with engine.connect() as connection:
        row = connection.execute(
            select(document_versions.c.id, blobs.c.sha256, blobs.c.ext)
            .select_from(
                documents.join(
                    document_versions, document_versions.c.id == documents.c.current_version_id
                ).join(blobs, blobs.c.sha256 == document_versions.c.blob_sha256)
            )
            .where(documents.c.id == document_id)
        ).one()
    return row.id, row.sha256, row.ext


def _chunks(engine: Engine, first_path: Mapping[str, str]) -> list[Chunk]:
    """Every stored chunk, as the scores see one: its document's path, page and text."""
    with engine.connect() as connection:
        rows = connection.execute(
            select(chunks.c.document_id, chunks.c.page_start, chunks.c.text).order_by(chunks.c.id)
        ).all()
    return [Chunk(first_path[row.document_id], row.page_start, row.text) for row in rows]


def _ranking(engine: Engine, query: str, first_path: Mapping[str, str]) -> list[Chunk]:
    """The first page of ``K`` results of keyword search for ``query``, each its best chunk."""
    page = search(engine, SearchQuery(query=query, limit=K))
    if not page.items:
        return []
    with engine.connect() as connection:
        rows = connection.execute(
            select(chunks.c.id, chunks.c.text).where(
                chunks.c.id.in_([hit.chunk_id for hit in page.items])
            )
        ).all()
    texts: dict[int, str] = {row.id: row.text for row in rows}
    return [Chunk(first_path[hit.document_id], hit.page, texts[hit.chunk_id]) for hit in page.items]
