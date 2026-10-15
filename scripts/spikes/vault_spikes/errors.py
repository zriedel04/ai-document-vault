"""Errors whose message was written in this package.

The command line prints the message of these and nothing else. Any other exception (a library's
own error, a failed read of a private folder) can carry a file name or a piece of a document in its
message, so the command line reports only its class (``__main__.main``). Raise one of these, with a
message that names no value read from a file, for anything the owner should be told about.
"""

from __future__ import annotations


class OwnMessage(Exception):  # noqa: N818 - a marker, not an error of its own
    """Marker: the message is fixed text of this package and is safe to print."""


class OwnValueError(OwnMessage, ValueError):
    """A value is not acceptable; the message says which kind, never the value."""


class OwnTypeError(OwnMessage, TypeError):
    """A value has the wrong type; the message says which kind, never the value."""


class OwnRuntimeError(OwnMessage, RuntimeError):
    """A step failed; the message says which, never what it was reading."""
