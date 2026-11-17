"""Extractor golden files: every fixture of ``extractor_golden`` gives what its golden file records.

A failure here means an extractor's output (or a fixture) changed. If the change is deliberate,
regenerate the golden files (``uv run python -m tests.unit.extractor_golden`` in ``backend/``),
review the diff and commit it with the change; if it is not, the extractor has drifted.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter

import pytest

from vault.ingest import extract_stage
from vault.jobs import sandbox

from . import extractor_golden
from .extractor_golden import DIRECT, FIXTURES, GOLDEN_DIR, REGENERATE, Fixture

BY_NAME = {fixture.name: fixture for fixture in FIXTURES}


def _load(fixture: Fixture) -> dict[str, object]:
    path = extractor_golden.golden_path(fixture.name)
    assert path.is_file(), f"{fixture.name} has no golden file: run {REGENERATE}"
    loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def test_every_fixture_has_one_golden_file_and_no_golden_file_is_stale() -> None:
    counts = Counter(fixture.name for fixture in FIXTURES)
    assert [name for name, count in counts.items() if count > 1] == []
    on_disk = {path.stem for path in GOLDEN_DIR.glob("*.json")}
    assert on_disk == set(counts), f"golden files and fixtures differ: run {REGENERATE}"
    assert {path.name for path in GOLDEN_DIR.iterdir()} == {f"{n}.json" for n in counts}


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
def test_extractor_output_matches_its_golden_file(fixture: Fixture) -> None:
    expected = _load(fixture)
    data = fixture.build()
    assert expected["input_sha256"] == hashlib.sha256(data).hexdigest(), (
        f"the fixture {fixture.name} changed since its golden file was written: run {REGENERATE}"
    )
    actual = extractor_golden.golden(fixture)
    assert actual == expected, f"the output for {fixture.name} drifted: see {REGENERATE}"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
def test_golden_files_are_written_by_the_generator(fixture: Fixture) -> None:
    """A hand edit that keeps the JSON valid would still be read; the file must be the
    generator's own text, so the documented command is the one way to change it."""
    path = extractor_golden.golden_path(fixture.name)
    text = path.read_bytes().decode("utf-8")
    assert text == extractor_golden.dumps(json.loads(text)), f"not generated: run {REGENERATE}"


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda fixture: fixture.name)
def test_recorded_offsets_point_at_the_recorded_text(fixture: Fixture) -> None:
    """The extractor's offset contract, read from the golden file itself: pages numbered from
    1 in order, and every heading inside its page's text and in order."""
    result = _load(fixture).get("result")
    if result is None:
        return
    assert isinstance(result, dict)
    pages = result["pages"]
    assert [page["number"] for page in pages] == list(range(1, len(pages) + 1))
    for page in pages:
        text = page["text"]
        previous_end = 0
        for heading in page["headings"]:
            assert previous_end <= heading["start"] < heading["end"] <= len(text)
            assert text[heading["start"] : heading["end"]] == heading["text_at_offsets"]
            assert heading["title"]
            assert heading["title"] == " ".join(heading["title"].split())
            previous_end = heading["end"]


def test_the_set_covers_every_extractor_the_stage_runs() -> None:
    """Each file type the extraction stage extracts has fixtures that it reads and that it
    refuses, and the functions called in this process are the sandbox's entry points."""
    for ext, extractor in extract_stage.EXTRACTORS.items():
        kinds = {"result" in _load(fixture) for fixture in FIXTURES if fixture.ext == ext}
        assert kinds == {True, False}, ext
        target = sandbox.ENTRY_POINTS[extractor.entry].target
        function = DIRECT[extractor.entry]
        assert f"{function.__module__}:{function.__name__}" == target
    assert {fixture.ext for fixture in FIXTURES} == set(extract_stage.EXTRACTORS)


def test_the_plan_s_fixture_kinds_are_in_the_set() -> None:
    """Plan section 13, Phase 2: multi-column PDF, headings, tables, non-English text, encrypted
    and corrupt files. Each is named here so that removing its fixture fails."""
    required = {
        "pdf-two-columns-by-column",
        "pdf-two-columns-by-row",
        "md-headings",
        "docx-headings",
        "md-table",
        "docx-tables",
        "txt-utf8-multilingual",
        "pdf-non-english",
        "pdf-encrypted",
        "docx-ole-container",
        "txt-utf8-cut-mid-character",
        "md-bad-utf8-after-mark",
        "docx-truncated",
        "docx-malformed-xml",
        "pdf-truncated",
        "pdf-garbage-body",
    }
    assert required <= set(BY_NAME)


def test_fixtures_are_reproducible() -> None:
    """Building a fixture twice gives the same bytes (no time, random or system in them)."""
    for fixture in FIXTURES:
        assert fixture.build() == fixture.build(), fixture.name


def test_stored_zip_does_not_depend_on_the_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """``zipfile`` records the creating system (0 on Windows, 3 elsewhere) unless told."""
    members = [("a.xml", b"<a/>")]
    here = extractor_golden.stored_zip(members)
    monkeypatch.setattr(sys, "platform", "linux" if sys.platform == "win32" else "win32")
    assert extractor_golden.stored_zip(members) == here
