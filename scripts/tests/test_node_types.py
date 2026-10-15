"""The Node.js type definitions match the oldest Node.js the frontend supports.

``@types/node`` describes one major version of Node.js. Types newer than the oldest supported
version let the editor and the type check accept an API that version does not have, and the
code then fails only when it runs there. So its major version must be the lowest major in
``engines.node`` and the version CI runs (standard library only).
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


def major(version: str) -> int:
    found = re.match(r"\D*(\d+)", version)
    if found is None:
        msg = f"no version number in {version!r}"
        raise ValueError(msg)
    return int(found.group(1))


class NodeTypesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.package = json.loads(PACKAGE.read_text(encoding="utf-8"))

    def test_types_match_the_oldest_supported_node(self) -> None:
        ranges = self.package["engines"]["node"].split("||")
        oldest = min(major(part) for part in ranges)
        self.assertEqual(major(self.package["devDependencies"]["@types/node"]), oldest)

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
            r"- dependency-name: \"@types/node\"\n\s+update-types: \[\"version-update:semver-major\"\]",
            text,
        )
        self.assertIsNotNone(rule, "dependabot.yml must ignore major updates of @types/node")


if __name__ == "__main__":
    unittest.main()
