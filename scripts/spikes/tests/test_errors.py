"""Every error raised by this package is one whose message the command line may print."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "vault_spikes"
# Built-in exception types whose message would be printed by nothing: the command line prints only
# ``OwnMessage`` errors in full, so a plain one is reported by its class alone. Raising one is a
# mistake unless the message is meant to be hidden, and then it needs a comment saying so.
PLAIN = {"ValueError", "TypeError", "RuntimeError", "OSError", "Exception", "KeyError"}


class OwnErrorsTest(unittest.TestCase):
    def test_no_module_raises_a_plain_error_with_a_message(self) -> None:
        offenders: list[str] = []
        for path in sorted(PACKAGE.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Raise)
                    and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name)
                    and node.exc.func.id in PLAIN
                ):
                    offenders.append(f"{path.name}:{node.lineno} raises {node.exc.func.id}")
        self.assertEqual(offenders, [], "use OwnValueError, OwnTypeError or OwnRuntimeError")


if __name__ == "__main__":
    unittest.main()
