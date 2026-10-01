"""Tests for scripts/check_adrs.py (standard library only)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import check_adrs  # noqa: E402

ADR = """# ADR {number}: Example

- Status: {status}
- Date: 2026-10-01
- Hardware-dependent: {hardware}
- Plan reference: docs/architecture.md, section 3
{extra}
## Context

Why.

## Decision

What.

## Alternatives considered

- Other.

## Consequences

Effects.
{evidence}"""

EVIDENCE = "\n## Evidence needed\n\nA spike.\n"


class CheckAdrsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / "docs" / "decisions"
        self.folder.mkdir(parents=True)

    def write_adr(
        self,
        number: int = 1,
        status: str = "Accepted",
        hardware: str = "no",
        extra: str = "",
        evidence: str = "",
        text: str | None = None,
    ) -> str:
        name = f"{number:04d}-example.md"
        content = text or ADR.format(
            number=f"{number:04d}",
            status=status,
            hardware=hardware,
            extra=extra,
            evidence=evidence,
        )
        (self.folder / name).write_text(content, encoding="utf-8")
        return name

    def write_index(self, rows: dict[str, str]) -> None:
        lines = ["| ADR | Decision | Status |", "| --- | --- | --- |"]
        for name, status in rows.items():
            lines.append(f"| [{name[:4]}]({name}) | Example | {status} |")
        (self.folder / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def problems(self) -> str:
        return "\n".join(check_adrs.check(self.root))

    def test_valid_adrs_pass(self) -> None:
        accepted = self.write_adr(1)
        proposed = self.write_adr(2, "Proposed", "yes", evidence=EVIDENCE)
        self.write_index({accepted: "Accepted", proposed: "Proposed"})
        self.assertEqual(check_adrs.check(self.root), [])

    def test_missing_section_is_reported(self) -> None:
        complete = ADR.format(
            number="0001", status="Accepted", hardware="no", extra="", evidence=""
        )
        name = self.write_adr(1, text=complete.replace("## Consequences\n\nEffects.\n", ""))
        self.write_index({name: "Accepted"})
        self.assertIn("'## Consequences'", self.problems())

    def test_bad_status_is_reported(self) -> None:
        name = self.write_adr(1, status="Done")
        self.write_index({name: "Done"})
        self.assertIn("status 'Done'", self.problems())

    def test_proposed_needs_evidence(self) -> None:
        name = self.write_adr(1, status="Proposed")
        self.write_index({name: "Proposed"})
        self.assertIn("Evidence needed", self.problems())

    def test_hardware_dependent_cannot_be_accepted_without_spike(self) -> None:
        name = self.write_adr(1, hardware="yes")
        self.write_index({name: "Accepted"})
        self.assertIn("cannot be Accepted without", self.problems())

    def test_hardware_dependent_accepted_with_spike_results(self) -> None:
        (self.root / "docs" / "spikes").mkdir(parents=True)
        (self.root / "docs" / "spikes" / "01-example.md").write_text("results\n")
        extra = "- Spike results: docs/spikes/01-example.md\n"
        name = self.write_adr(1, hardware="yes", extra=extra)
        self.write_index({name: "Accepted"})
        self.assertEqual(check_adrs.check(self.root), [])

    def test_spike_link_must_exist(self) -> None:
        extra = "- Spike results: docs/spikes/missing.md\n"
        name = self.write_adr(1, hardware="yes", extra=extra)
        self.write_index({name: "Accepted"})
        self.assertIn("cannot be Accepted without", self.problems())

    def test_index_must_list_every_adr_with_matching_status(self) -> None:
        first = self.write_adr(1)
        second = self.write_adr(2)
        self.write_index({first: "Proposed"})
        problems = self.problems()
        self.assertIn(f"{second} is not listed", problems)
        self.assertIn("'Proposed' in the index but 'Accepted' in the file", problems)

    def test_numbers_must_not_have_gaps(self) -> None:
        name = self.write_adr(2)
        self.write_index({name: "Accepted"})
        self.assertIn("without gaps", self.problems())

    def test_superseded_status_is_allowed(self) -> None:
        old = self.write_adr(1, status="Superseded by ADR 0002")
        new = self.write_adr(2)
        self.write_index({old: "Superseded by ADR 0002", new: "Accepted"})
        self.assertEqual(check_adrs.check(self.root), [])

    def test_superseded_by_missing_adr_is_reported(self) -> None:
        name = self.write_adr(1, status="Superseded by ADR 9999")
        self.write_index({name: "Superseded by ADR 9999"})
        self.assertIn("must name an existing, newer ADR", self.problems())

    def test_superseded_by_itself_or_older_adr_is_reported(self) -> None:
        first = self.write_adr(1)
        second = self.write_adr(2, status="Superseded by ADR 0001")
        third = self.write_adr(3, status="Superseded by ADR 0003")
        self.write_index(
            {first: "Accepted", second: "Superseded by ADR 0001", third: "Superseded by ADR 0003"}
        )
        problems = check_adrs.check(self.root)
        self.assertEqual(sum("must name an existing, newer ADR" in p for p in problems), 2)

    def test_spike_link_must_be_a_spike_result_file(self) -> None:
        spikes = self.root / "docs" / "spikes"
        spikes.mkdir(parents=True)
        (spikes / "README.md").write_text("index\n")
        (spikes / "01-example.md").write_text("results\n")
        (spikes / "nested").mkdir()
        (spikes / "nested" / "02-example.md").write_text("results\n")
        (self.root / "docs" / "architecture.md").write_text("plan\n")
        bad_links = (
            "docs/architecture.md",
            "docs/spikes/README.md",
            "docs/spikes/nested/02-example.md",
            "docs/spikes/../architecture.md",
            "docs/spikes/01-example.MD",
            str(self.root / "docs" / "spikes" / "01-example.md"),
            "/etc/passwd",
            "",
        )
        for link in bad_links:
            with self.subTest(link=link):
                for old in self.folder.glob("0*.md"):
                    old.unlink()
                name = self.write_adr(1, hardware="yes", extra=f"- Spike results: {link}\n")
                self.write_index({name: "Accepted"})
                self.assertIn("cannot be Accepted without", self.problems())

    def test_spike_link_may_not_escape_through_a_symlink(self) -> None:
        spikes = self.root / "docs" / "spikes"
        spikes.mkdir(parents=True)
        (self.root / "docs" / "architecture.md").write_text("plan\n")
        try:
            (spikes / "01-link.md").symlink_to(self.root / "docs" / "architecture.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        name = self.write_adr(1, hardware="yes", extra="- Spike results: docs/spikes/01-link.md\n")
        self.write_index({name: "Accepted"})
        self.assertIn("cannot be Accepted without", self.problems())

    def test_malformed_file_names_are_reported(self) -> None:
        name = self.write_adr(1)
        self.write_index({name: "Accepted"})
        for stray in ("0002_new-decision.md", "0002-New-Decision.md", "0002-new.MD", "notes.md"):
            with self.subTest(stray=stray):
                path = self.folder / stray
                path.write_text("# stray\n")
                self.assertIn(f"{stray}: not a valid ADR file name", self.problems())
                path.unlink()

    def test_readme_and_template_are_not_strays(self) -> None:
        name = self.write_adr(1)
        self.write_index({name: "Accepted"})
        (self.folder / "0000-template.md").write_text("# template\n")
        self.assertEqual(check_adrs.check(self.root), [])

    def test_repository_adrs_pass(self) -> None:
        repository = SCRIPTS.parent
        self.assertEqual(check_adrs.check(repository), [])


if __name__ == "__main__":
    unittest.main()
