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

SPIKE = "".join(
    f"## {name}\n\nFilled in.\n\n"
    for name in ("Question", "Environment", "Method", "Results", "Conclusion")
)


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
        (self.root / "docs" / "spikes" / "01-example.md").write_text(SPIKE)
        extra = "- Spike results: docs/spikes/01-example.md\n"
        name = self.write_adr(1, hardware="yes", extra=extra)
        self.write_index({name: "Accepted"})
        self.assertEqual(check_adrs.check(self.root), [])

    def test_spike_file_needs_every_section_filled_in(self) -> None:
        spikes = self.root / "docs" / "spikes"
        spikes.mkdir(parents=True)
        empty_results = SPIKE.replace("## Results\n\nFilled in.\n\n", "## Results\n\n")
        contents = {
            "one line": "results\n",
            "empty file": "",
            "missing Environment": SPIKE.replace("## Environment\n\nFilled in.\n\n", ""),
            "empty Results": empty_results,
            "wrong heading level": SPIKE.replace("## Method", "### Method"),
        }
        for label, content in contents.items():
            with self.subTest(label=label):
                (spikes / "01-example.md").write_text(content)
                for old in self.folder.glob("0*.md"):
                    old.unlink()
                extra = "- Spike results: docs/spikes/01-example.md\n"
                name = self.write_adr(1, hardware="yes", extra=extra)
                self.write_index({name: "Accepted"})
                self.assertIn("non-empty Question, Environment", self.problems())

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

    def test_index_row_number_must_match_the_linked_file(self) -> None:
        name = self.write_adr(1)
        rows = ["| ADR | Decision | Status |", "| --- | --- | --- |"]
        rows.append(f"| [9999]({name}) | Example | Accepted |")
        (self.folder / "README.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.assertIn(f"row [9999]({name}) shows a number that does not match", self.problems())

    def test_numbers_must_not_have_gaps(self) -> None:
        name = self.write_adr(2)
        self.write_index({name: "Accepted"})
        self.assertIn("without gaps", self.problems())

    def test_superseded_status_is_allowed(self) -> None:
        old = self.write_adr(1, status="Superseded by ADR 0002")
        new = self.write_adr(2, extra="- Supersedes: ADR 0001\n")
        self.write_index({old: "Superseded by ADR 0002", new: "Accepted"})
        self.assertEqual(check_adrs.check(self.root), [])

    def test_a_chain_of_supersessions_is_allowed(self) -> None:
        first = self.write_adr(1, status="Superseded by ADR 0002")
        second = self.write_adr(
            2, status="Superseded by ADR 0003", extra="- Supersedes: ADR 0001\n"
        )
        third = self.write_adr(3, extra="- Supersedes: ADR 0002\n")
        self.write_index(
            {first: "Superseded by ADR 0002", second: "Superseded by ADR 0003", third: "Accepted"}
        )
        self.assertEqual(check_adrs.check(self.root), [])

    def test_one_adr_may_supersede_several(self) -> None:
        first = self.write_adr(1, status="Superseded by ADR 0003")
        second = self.write_adr(2, status="Superseded by ADR 0003")
        third = self.write_adr(3, extra="- Supersedes: ADR 0001, ADR 0002\n")
        self.write_index(
            {first: "Superseded by ADR 0003", second: "Superseded by ADR 0003", third: "Accepted"}
        )
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

    def test_replacement_must_be_accepted(self) -> None:
        for status in ("Proposed", "Rejected"):
            with self.subTest(status=status):
                for old in self.folder.glob("0*.md"):
                    old.unlink()
                evidence = EVIDENCE if status == "Proposed" else ""
                first = self.write_adr(1, status="Superseded by ADR 0002")
                second = self.write_adr(
                    2, status=status, extra="- Supersedes: ADR 0001\n", evidence=evidence
                )
                self.write_index({first: "Superseded by ADR 0002", second: status})
                self.assertIn(f"is '{status}', it must be Accepted", self.problems())

    def test_replacement_must_say_it_supersedes_the_old_adr(self) -> None:
        """An unrelated Accepted ADR cannot be used to retire a decision."""
        first = self.write_adr(1, status="Superseded by ADR 0003")
        second = self.write_adr(2)
        third = self.write_adr(3, extra="- Supersedes: ADR 0002\n")
        self.write_index(
            {first: "Superseded by ADR 0003", second: "Accepted", third: "Accepted"}
        )
        problems = self.problems()
        self.assertIn("0001: ADR 0003 must say '- Supersedes: ADR 0001'", problems)
        self.assertIn("0003: supersedes ADR 0002, whose status must be", problems)

    def test_supersedes_without_the_old_status_change_is_reported(self) -> None:
        first = self.write_adr(1)
        second = self.write_adr(2, extra="- Supersedes: ADR 0001\n")
        self.write_index({first: "Accepted", second: "Accepted"})
        self.assertIn("supersedes ADR 0001, whose status must be", self.problems())

    def test_supersedes_must_name_an_older_existing_adr(self) -> None:
        first = self.write_adr(1, extra="- Supersedes: ADR 0001\n")
        second = self.write_adr(2, extra="- Supersedes: ADR 0009\n")
        self.write_index({first: "Accepted", second: "Accepted"})
        problems = check_adrs.check(self.root)
        self.assertEqual(sum("must name an older ADR" in p for p in problems), 2)

    def test_malformed_supersedes_line_is_reported(self) -> None:
        first = self.write_adr(1)
        second = self.write_adr(2, extra="- Supersedes: 0001\n")
        self.write_index({first: "Accepted", second: "Accepted"})
        self.assertIn("'- Supersedes:' must look like", self.problems())

    def test_spike_link_must_be_a_spike_result_file(self) -> None:
        spikes = self.root / "docs" / "spikes"
        spikes.mkdir(parents=True)
        (spikes / "README.md").write_text(SPIKE)
        (spikes / "01-example.md").write_text(SPIKE)
        (spikes / "nested").mkdir()
        (spikes / "nested" / "02-example.md").write_text(SPIKE)
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
        (self.root / "docs" / "architecture.md").write_text(SPIKE)
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
