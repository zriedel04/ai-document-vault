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
        name = self.write_adr(1, status="Superseded by ADR 0002")
        self.write_index({name: "Superseded by ADR 0002"})
        self.assertEqual(check_adrs.check(self.root), [])

    def test_repository_adrs_pass(self) -> None:
        repository = SCRIPTS.parent
        self.assertEqual(check_adrs.check(repository), [])


if __name__ == "__main__":
    unittest.main()
