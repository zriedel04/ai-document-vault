"""Write the API's OpenAPI schema, from which the frontend's typed client is generated.

Usage: ``python -m vault.openapi ../frontend/openapi.json``. CI regenerates the file and fails if
it differs from the committed copy, so the frontend never drifts from the backend.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

from vault.core.config import Settings
from vault.main import create_app


def schema_text() -> str:
    # Defaults only: the schema must not depend on a local config file or environment.
    schema = create_app(Settings(), serve_frontend=False).openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m vault.openapi OUTPUT.json", file=sys.stderr)
        return 2
    Path(args[0]).write_text(schema_text(), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
