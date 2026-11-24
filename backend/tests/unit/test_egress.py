"""The egress gate (``vault.ai.egress``) on a synthetic vault: what may leave, what may not."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, delete, insert, select, text, update

from tests.unit.test_documents_schema import NOW, add_document
from vault.ai import egress
from vault.ai.egress import EgressDecision, EgressMode, Withheld, check_egress
from vault.core.logging import SafeFormatter
from vault.db import ids
from vault.db.chunks import replace_chunks
from vault.db.engine import create_vault_engine
from vault.db.labels import name_key
from vault.db.migrate import upgrade_to_latest
from vault.db.pages import PageText, replace_pages
from vault.db.schema import chunks, document_fields, document_labels, documents, labels
from vault.ingest.chunking.structural_v1 import CHUNKER_VERSION, chunk_pages

CANARY = "canary-egress-text-xyzzy"
TITLE = "canary-egress-title-plugh"
LABEL = "canary-egress-label-quux"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db_file = tmp_path / "vault.db"
    upgrade_to_latest(db_file)
    engine = create_vault_engine(db_file)
    try:
        yield engine
    finally:
        engine.dispose()


def add_chunks(engine: Engine, pages: int = 1) -> tuple[str, list[int]]:
    """A document whose current version has ``pages`` pages of synthetic text, chunked."""
    document_id, version_id = add_document(engine)
    stored = [
        PageText(number, f"Page {number} of a synthetic note. {CANARY} {number}.\n")
        for number in range(1, pages + 1)
    ]
    replace_pages(engine, version_id, stored, "text-1", "utf-8")
    replace_chunks(engine, version_id, chunk_pages(stored), CHUNKER_VERSION)
    with engine.connect() as connection:
        chunk_ids = connection.execute(
            select(chunks.c.id).where(chunks.c.document_id == document_id).order_by(chunks.c.id)
        ).scalars()
        return document_id, list(chunk_ids)


def one_chunk(engine: Engine) -> tuple[str, int]:
    document_id, chunk_ids = add_chunks(engine)
    assert len(chunk_ids) == 1
    return document_id, chunk_ids[0]


def set_document(engine: Engine, document_id: str, **values: Any) -> None:
    with engine.begin() as connection:
        connection.execute(update(documents).where(documents.c.id == document_id).values(values))


def trash(engine: Engine, document_id: str) -> None:
    set_document(
        engine, document_id, state="trashed", trashed_at=NOW, state_before_trash="processing"
    )


def add_label(
    engine: Engine, *, parent: str | None = None, local_only: bool = False, name: str = LABEL
) -> str:
    label_id = ids.new_ulid()
    with engine.begin() as connection:
        connection.execute(
            insert(labels).values(
                id=label_id,
                kind="collection",
                name=name,
                name_key=name_key(name),
                parent_id=parent,
                local_only=local_only,
            )
        )
    return label_id


def assign(engine: Engine, document_id: str, label_id: str, status: str = "accepted") -> None:
    with engine.begin() as connection:
        connection.execute(
            insert(document_labels).values(
                document_id=document_id,
                label_id=label_id,
                source="user",
                status=status,
                created_at=NOW,
            )
        )


def decide(engine: Engine, chunk_ids: list[int], mode: EgressMode) -> EgressDecision:
    return check_egress(engine, chunk_ids, mode)


# --- what may be sent --------------------------------------------------------------------------


def test_chunks_of_ordinary_documents_are_all_allowed(engine: Engine) -> None:
    _, first = add_chunks(engine, pages=2)
    _, second = add_chunks(engine)
    wanted = [second[0], *first]
    for mode in EgressMode:
        decision = decide(engine, wanted, mode)
        assert decision.allowed == tuple(wanted)
        assert decision.withheld == ()
        assert not decision.refused
        assert decision.duplicates == 0
        assert decision.mode is mode


def test_empty_input_sends_nothing_and_refuses_nothing(engine: Engine) -> None:
    for mode in EgressMode:
        assert decide(engine, [], mode) == EgressDecision(mode, (), (), False, 0)


def test_all_local_only_withholds_everything(engine: Engine) -> None:
    first, first_chunk = one_chunk(engine)
    second, second_chunk = one_chunk(engine)
    set_document(engine, first, local_only=True)
    set_document(engine, second, local_only=True)
    expected = (
        (first_chunk, Withheld.DOCUMENT_LOCAL_ONLY),
        (second_chunk, Withheld.DOCUMENT_LOCAL_ONLY),
    )
    dropped = decide(engine, [first_chunk, second_chunk], EgressMode.DROP)
    assert dropped.allowed == ()
    assert dropped.withheld == expected
    assert not dropped.refused
    refused = decide(engine, [first_chunk, second_chunk], EgressMode.REFUSE)
    assert refused.allowed == ()
    assert refused.withheld == expected
    assert refused.refused


def test_mixed_drop_sends_the_rest_and_refuse_sends_nothing(engine: Engine) -> None:
    _, open_chunk = one_chunk(engine)
    private, private_chunk = one_chunk(engine)
    trashed, trashed_chunk = one_chunk(engine)
    labelled, labelled_chunk = one_chunk(engine)
    set_document(engine, private, local_only=True)
    trash(engine, trashed)
    assign(engine, labelled, add_label(engine, local_only=True))
    wanted = [private_chunk, open_chunk, trashed_chunk, labelled_chunk]
    expected_withheld = (
        (private_chunk, Withheld.DOCUMENT_LOCAL_ONLY),
        (trashed_chunk, Withheld.TRASHED),
        (labelled_chunk, Withheld.LABEL_LOCAL_ONLY),
    )

    dropped = decide(engine, wanted, EgressMode.DROP)
    assert dropped.allowed == (open_chunk,)
    assert dropped.withheld == expected_withheld
    assert not dropped.refused

    refused = decide(engine, wanted, EgressMode.REFUSE)
    assert refused.allowed == ()
    assert refused.withheld == expected_withheld
    assert refused.refused  # the open document's chunk passed but is not sent either


def test_a_trashed_local_only_document_reports_the_trash_first(engine: Engine) -> None:
    document_id, chunk_id = one_chunk(engine)
    set_document(engine, document_id, local_only=True)
    trash(engine, document_id)
    assert decide(engine, [chunk_id], EgressMode.DROP).withheld == ((chunk_id, Withheld.TRASHED),)


def test_every_live_state_may_be_sent(engine: Engine) -> None:
    document_id, chunk_id = one_chunk(engine)
    for state in ("processing", "searchable", "ready", "failed"):
        set_document(engine, document_id, state=state)
        assert decide(engine, [chunk_id], EgressMode.REFUSE).allowed == (chunk_id,)


# --- labels -----------------------------------------------------------------------------------


def test_a_local_only_label_above_the_documents_label_withholds(engine: Engine) -> None:
    document_id, chunk_id = one_chunk(engine)
    top = add_label(engine, local_only=True)
    middle = add_label(engine, parent=top, name="middle")
    assign(engine, document_id, add_label(engine, parent=middle, name="leaf"))
    decision = decide(engine, [chunk_id], EgressMode.DROP)
    assert decision.withheld == ((chunk_id, Withheld.LABEL_LOCAL_ONLY),)


def test_a_local_only_label_below_the_documents_label_does_not_withhold(engine: Engine) -> None:
    document_id, chunk_id = one_chunk(engine)
    top = add_label(engine)
    add_label(engine, parent=top, local_only=True, name="private child")
    assign(engine, document_id, top)
    assert decide(engine, [chunk_id], EgressMode.REFUSE).allowed == (chunk_id,)


def test_a_suggested_label_counts_and_a_rejected_one_does_not(engine: Engine) -> None:
    suggested, suggested_chunk = one_chunk(engine)
    rejected, rejected_chunk = one_chunk(engine)
    private = add_label(engine, local_only=True)
    with engine.begin() as connection:
        for document_id, status in ((suggested, "suggested"), (rejected, "rejected")):
            connection.execute(
                insert(document_labels).values(
                    document_id=document_id,
                    label_id=private,
                    source="ai",
                    status=status,
                    confidence=0.5,
                    model="synthetic-model",
                    created_at=NOW,
                )
            )
    decision = decide(engine, [suggested_chunk, rejected_chunk], EgressMode.DROP)
    assert decision.allowed == (rejected_chunk,)
    assert decision.withheld == ((suggested_chunk, Withheld.LABEL_LOCAL_ONLY),)


def test_a_parent_loop_ends_and_a_local_only_label_in_it_still_counts(engine: Engine) -> None:
    document_id, chunk_id = one_chunk(engine)
    first = add_label(engine, name="first")
    second = add_label(engine, parent=first, name="second", local_only=True)
    with engine.begin() as connection:
        connection.execute(update(labels).where(labels.c.id == first).values(parent_id=second))
    assign(engine, document_id, first)
    decision = decide(engine, [chunk_id], EgressMode.DROP)
    assert decision.withheld == ((chunk_id, Withheld.LABEL_LOCAL_ONLY),)


def test_a_label_missing_from_its_table_withholds(engine: Engine) -> None:
    document_id, chunk_id = one_chunk(engine)
    label_id = add_label(engine)
    assign(engine, document_id, label_id)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(delete(labels).where(labels.c.id == label_id))
        connection.commit()
    decision = decide(engine, [chunk_id], EgressMode.DROP)
    assert decision.withheld == ((chunk_id, Withheld.LABEL_LOCAL_ONLY),)


def test_a_missing_parent_label_withholds(engine: Engine) -> None:
    document_id, chunk_id = one_chunk(engine)
    parent = add_label(engine, name="parent")
    assign(engine, document_id, add_label(engine, parent=parent, name="child"))
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(delete(labels).where(labels.c.id == parent))
        connection.commit()
    decision = decide(engine, [chunk_id], EgressMode.DROP)
    assert decision.withheld == ((chunk_id, Withheld.LABEL_LOCAL_ONLY),)


# --- unknown, repeated and invalid ids ------------------------------------------------------------


def test_unknown_ids_are_withheld(engine: Engine) -> None:
    _, chunk_id = one_chunk(engine)
    unknown = chunk_id + 1000
    dropped = decide(engine, [unknown, chunk_id], EgressMode.DROP)
    assert dropped.allowed == (chunk_id,)
    assert dropped.withheld == ((unknown, Withheld.UNKNOWN_CHUNK),)
    refused = decide(engine, [chunk_id, unknown], EgressMode.REFUSE)
    assert refused.refused
    assert refused.allowed == ()


def test_a_chunk_whose_document_row_is_missing_is_withheld(engine: Engine) -> None:
    document_id, chunk_id = one_chunk(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(delete(documents).where(documents.c.id == document_id))
        connection.commit()
    decision = decide(engine, [chunk_id], EgressMode.DROP)
    assert decision.withheld == ((chunk_id, Withheld.MISSING_DOCUMENT),)


def test_duplicates_are_checked_and_sent_once_in_first_order(engine: Engine) -> None:
    _, first = one_chunk(engine)
    private, second = one_chunk(engine)
    set_document(engine, private, local_only=True)
    decision = decide(engine, [first, second, first, second, first], EgressMode.DROP)
    assert decision.allowed == (first,)
    assert decision.withheld == ((second, Withheld.DOCUMENT_LOCAL_ONLY),)
    assert decision.duplicates == 3


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.0, "1", None])
def test_an_id_that_is_not_a_positive_int_raises(engine: Engine, bad: object) -> None:
    with pytest.raises(ValueError, match="positive int"):
        check_egress(engine, [bad], EgressMode.DROP)  # type: ignore[list-item]


def test_a_mode_that_is_not_an_egress_mode_raises(engine: Engine) -> None:
    with pytest.raises(ValueError, match="EgressMode"):
        check_egress(engine, [], "drop")  # type: ignore[arg-type]


def test_more_ids_than_one_batch(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(egress, "BATCH", 2)
    _, open_chunks = add_chunks(engine, pages=3)
    private, private_chunks = add_chunks(engine, pages=3)
    set_document(engine, private, local_only=True)
    labelled, labelled_chunks = add_chunks(engine, pages=2)
    assign(engine, labelled, add_label(engine, local_only=True))
    wanted = [*private_chunks, *open_chunks, *labelled_chunks, 99_999]
    decision = decide(engine, wanted, EgressMode.DROP)
    assert decision.allowed == tuple(open_chunks)
    assert dict(decision.withheld) == {
        **dict.fromkeys(private_chunks, Withheld.DOCUMENT_LOCAL_ONLY),
        **dict.fromkeys(labelled_chunks, Withheld.LABEL_LOCAL_ONLY),
        99_999: Withheld.UNKNOWN_CHUNK,
    }


# --- failures ---------------------------------------------------------------------------------


def test_a_database_error_withholds_everything(engine: Engine, tmp_path: Path) -> None:
    _, chunk_id = one_chunk(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE document_labels"))
    for mode in EgressMode:
        decision = decide(engine, [chunk_id, chunk_id + 1], mode)
        assert decision.allowed == ()
        assert decision.withheld == (
            (chunk_id, Withheld.DATABASE_ERROR),
            (chunk_id + 1, Withheld.DATABASE_ERROR),
        )
        assert decision.refused is (mode is EgressMode.REFUSE)


def test_a_database_that_cannot_be_opened_withholds_everything(tmp_path: Path) -> None:
    folder = tmp_path / "not-a-database"
    folder.mkdir()
    engine = create_vault_engine(folder)  # a folder: SQLite cannot open it
    try:
        decision = check_egress(engine, [1, 2], EgressMode.DROP)
    finally:
        engine.dispose()
    assert decision.allowed == ()
    assert decision.withheld == ((1, Withheld.DATABASE_ERROR), (2, Withheld.DATABASE_ERROR))


def test_another_error_propagates_and_nothing_is_sent(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object) -> None:
        raise OSError("synthetic failure")

    monkeypatch.setattr(egress, "_reasons", broken)
    with pytest.raises(OSError, match="synthetic failure"):
        check_egress(engine, [1], EgressMode.DROP)


# --- logs -------------------------------------------------------------------------------------


def lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    formatter = SafeFormatter()
    return [formatter.format(record) for record in caplog.records if record.name == egress.__name__]


def test_the_log_holds_ids_and_counts_and_no_document_text(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    open_doc, open_chunk = one_chunk(engine)
    private, private_chunk = one_chunk(engine)
    set_document(engine, private, local_only=True)
    label_id = add_label(engine, name=LABEL)
    assign(engine, open_doc, label_id)
    with engine.begin() as connection:
        connection.execute(
            insert(document_fields).values(
                document_id=open_doc,
                field="title",
                source="file_properties",
                version_id=connection.execute(
                    select(documents.c.current_version_id).where(documents.c.id == open_doc)
                ).scalar_one(),
                value=TITLE,
            )
        )
    caplog.set_level(logging.INFO, logger=egress.__name__)
    decide(engine, [open_chunk, private_chunk, open_chunk, 777_777], EgressMode.DROP)

    written = lines(caplog)
    summary = next(line for line in written if " ai.egress " in line)
    assert summary.endswith(" mode=drop requested=4 duplicates=1 sent=1 withheld=2 refused=False")
    per_chunk = [line for line in written if " ai.egress_chunk " in line]
    assert [line.split(" ai.egress_chunk ")[1] for line in per_chunk] == [
        f"chunk_id={open_chunk} sent=True reason=None",
        f"chunk_id={private_chunk} sent=False reason=document_local_only",
    ]
    # An id that names no chunk is counted, never written.
    assert not any("777777" in line for line in written)
    everything = "\n".join(written) + "\n".join(
        f"{record.msg} {record.args} {record.__dict__}" for record in caplog.records
    )
    for private_text in (CANARY, TITLE, LABEL, "Syllabus", "Owner", open_doc, private, label_id):
        assert private_text not in everything
    assert "[redacted]" not in everything


def test_a_database_error_is_logged_by_class_name_only(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    _, chunk_id = one_chunk(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE document_labels"))
    caplog.set_level(logging.INFO, logger=egress.__name__)
    decide(engine, [chunk_id], EgressMode.REFUSE)
    failed = next(line for line in lines(caplog) if " ai.egress_failed " in line)
    assert "exc_type=OperationalError" in failed
    assert "document_labels" not in failed
    assert " chunks=1" in failed
