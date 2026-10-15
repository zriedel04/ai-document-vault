"""Row ids: ULIDs (https://github.com/ulid/spec), written by the application, not by SQLite.

A ULID is 26 characters of Crockford's base32: a 48-bit millisecond timestamp, then 80 random
bits. Ids made later sort after ids made earlier (to the millisecond), so a list ordered by id is
in creation order, and an id says nothing about the document it names. Within one millisecond the
order is random, which is enough here: nothing relies on it.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Final

ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LENGTH: Final = 26
# The first character carries only 3 bits of the 48-bit time, so it is 0 to 7. Use fullmatch.
ULID: Final = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_TIME_BITS: Final = 48
_RANDOM_BITS: Final = 80


def ulid_check(column: str) -> str:
    """The same rule as SQL for a CHECK constraint. GLOB is case-sensitive, and has no repeat."""
    return (
        f"length({column}) = 26 AND substr({column}, 1, 1) BETWEEN '0' AND '7'"
        f" AND {column} NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'"
    )


def new_ulid(now_ms: int | None = None) -> str:
    """A new ULID for the current time, or for ``now_ms`` (milliseconds since 1970, for tests)."""
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    if not 0 <= now_ms < 1 << _TIME_BITS:
        msg = "a ULID time must fit in 48 bits"
        raise ValueError(msg)
    value = (now_ms << _RANDOM_BITS) | secrets.randbits(_RANDOM_BITS)
    characters = []
    for _ in range(LENGTH):
        characters.append(ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(characters))


def is_ulid(value: object) -> bool:
    """True if ``value`` is a ULID as this module writes it (upper case, 26 characters)."""
    return isinstance(value, str) and ULID.fullmatch(value) is not None
