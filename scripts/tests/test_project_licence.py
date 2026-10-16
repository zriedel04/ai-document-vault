"""Every package this repository builds carries the project licence text (Apache-2.0 section 4(a)).

A wheel or sdist holds only the licence files its `pyproject.toml` lists in `license-files`, read
relative to that file, so a package in a subfolder needs its own copy of the root `LICENSE`.
Standard library only.
"""

from __future__ import annotations

import os
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIPPED = {"node_modules", ".venv", ".git", ".claude"}


def projects() -> list[Path]:
    found = []
    for folder, folders, files in os.walk(ROOT):
        folders[:] = [name for name in folders if name not in SKIPPED]
        if "pyproject.toml" in files:
            found.append(Path(folder) / "pyproject.toml")
    return sorted(found)


class ProjectLicenceTest(unittest.TestCase):
    def test_every_built_package_ships_the_root_licence(self) -> None:
        licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
        built = []
        for path in projects():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            if "build-system" not in data:
                continue  # never built into a wheel or sdist
            built.append(path)
            project = data["project"]
            with self.subTest(project=str(path.relative_to(ROOT))):
                self.assertEqual(project.get("license"), "Apache-2.0")
                files = project.get("license-files")
                self.assertEqual(files, ["LICENSE"])
                copy = path.parent / "LICENSE"
                self.assertTrue(copy.is_file(), f"{copy.relative_to(ROOT)} is missing")
                self.assertEqual(copy.read_text(encoding="utf-8"), licence)
        self.assertIn(ROOT / "backend" / "pyproject.toml", built)  # the check found the backend


if __name__ == "__main__":
    unittest.main()
