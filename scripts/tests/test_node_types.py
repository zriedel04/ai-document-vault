"""The Node.js type definitions match the oldest Node.js the frontend supports.

``@types/node`` follows Node.js releases: 22.13 types describe the APIs of Node 22.13. Types newer
than the oldest supported release let the type check accept an API that release does not have,
and the code then fails only when it runs there. So the types' major and minor are those of the
oldest release ``engines.node`` allows, and CI runs that major (standard library only).
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "frontend" / "package.json"
WORKFLOWS = ROOT / ".github" / "workflows"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"


def release(version: str) -> tuple[int, int]:
    """The major and minor of a version or of the start of a range (``^22.13.0`` is 22.13)."""
    found = re.match(r"\D*(\d+)(?:\.(\d+))?", version)
    if found is None:
        msg = f"no version number in {version!r}"
        raise ValueError(msg)
    return int(found.group(1)), int(found.group(2) or 0)


def major(version: str) -> int:
    return release(version)[0]


class NodeTypesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.package = json.loads(PACKAGE.read_text(encoding="utf-8"))

    def test_types_match_the_oldest_supported_node_release(self) -> None:
        ranges = self.package["engines"]["node"].split("||")
        oldest = min(release(part.strip()) for part in ranges)
        self.assertEqual(release(self.package["devDependencies"]["@types/node"]), oldest)

    def test_a_release_is_read_with_its_minor(self) -> None:
        self.assertEqual(release("^22.13.0"), (22, 13))
        self.assertEqual(release(">=26.0.0"), (26, 0))
        self.assertEqual(release("22.20.4"), (22, 20))
        self.assertEqual(release("22"), (22, 0))

    def test_ci_runs_the_node_the_types_describe(self) -> None:
        types = major(self.package["devDependencies"]["@types/node"])
        versions = [
            major(value)
            for workflow in sorted(WORKFLOWS.glob("*.yml"))
            for value in re.findall(
                r'node-version:\s*"?([^"\s]+)', workflow.read_text(encoding="utf-8")
            )
        ]
        self.assertTrue(versions, "no workflow sets up Node.js")
        self.assertEqual(set(versions), {types})

    def test_dependabot_does_not_move_the_types_to_another_major(self) -> None:
        text = DEPENDABOT.read_text(encoding="utf-8")
        rule = re.search(
            r"- dependency-name: \"@types/node\"\n\s+update-types:"
            r" \[\"version-update:semver-major\", \"version-update:semver-minor\"\]",
            text,
        )
        self.assertIsNotNone(rule, "dependabot.yml must ignore major and minor @types/node updates")


if __name__ == "__main__":
    unittest.main()
