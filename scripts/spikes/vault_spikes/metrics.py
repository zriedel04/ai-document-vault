"""Pure functions for the numbers the spikes report. Standard library only."""

from __future__ import annotations

import math
import re
import sys
from collections.abc import Hashable, Sequence

_SPACE = re.compile(r"\s+")


def normalise_text(text: str) -> str:
    """Collapse every run of whitespace to one space and trim, so layout does not count as error."""
    return _SPACE.sub(" ", text).strip()


def edit_distance[T: Hashable](a: Sequence[T], b: Sequence[T]) -> int:
    """Levenshtein distance between two sequences (insert, delete, substitute: cost 1 each)."""
    if len(a) < len(b):
        a, b = b, a  # keep the inner row as short as possible
    previous = list(range(len(b) + 1))
    for i, item_a in enumerate(a, start=1):
        current = [i]
        for j, item_b in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (item_a != item_b))
            )
        previous = current
    return previous[-1]


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Edit distance over characters divided by the reference length (after whitespace cleanup).

    An empty reference has no defined rate: it raises ValueError rather than reporting 0 or 1.
    """
    ref = normalise_text(reference)
    if not ref:
        msg = "the reference text is empty"
        raise ValueError(msg)
    return edit_distance(ref, normalise_text(hypothesis)) / len(ref)


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Edit distance over words divided by the number of reference words."""
    ref = normalise_text(reference).split(" ")
    if ref == [""]:
        msg = "the reference text is empty"
        raise ValueError(msg)
    hyp = normalise_text(hypothesis).split(" ") if normalise_text(hypothesis) else []
    return edit_distance(ref, hyp) / len(ref)


def mean(values: Sequence[float]) -> float:
    if not values:
        msg = "no values"
        raise ValueError(msg)
    return math.fsum(values) / len(values)


def percentile(values: Sequence[float], fraction: float) -> float:
    """The value below which ``fraction`` (0 to 1) of the data falls, by linear interpolation."""
    if not values:
        msg = "no values"
        raise ValueError(msg)
    if not 0 <= fraction <= 1:
        msg = "fraction must be between 0 and 1"
        raise ValueError(msg)
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def median(values: Sequence[float]) -> float:
    return percentile(values, 0.5)


def per_second(count: float, seconds: float) -> float:
    """``count / seconds``; zero time is an error, not infinity."""
    if seconds <= 0:
        msg = "elapsed time must be positive"
        raise ValueError(msg)
    return count / seconds


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors of the same length (0.0 if either has no length)."""
    if len(a) != len(b):
        msg = "vectors must have the same length"
        raise ValueError(msg)
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(math.fsum(x * x for x in a)) * math.sqrt(math.fsum(y * y for y in b))
    return dot / norm if norm else 0.0


def peak_memory_mib() -> float | None:
    """The most memory this process has used so far, in MiB, or None if the OS will not say.

    It only ever grows, so measure one candidate per process for a number that means something.
    """
    if sys.platform == "win32":  # pragma: no cover - exercised on the Windows runner
        return _peak_memory_windows()
    try:
        import resource
    except ImportError:  # pragma: no cover
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def gpu_peak_memory_mib() -> tuple[float, float] | None:
    """The most memory this process has had on the first CUDA GPU, in MiB, as (allocated, reserved),
    or None when torch or a GPU is missing.

    ``peak_memory_mib`` is the host's working set and never includes video memory, so a model that
    nearly fills the card would look small without this. Allocated is what the tensors needed;
    reserved is what torch's allocator took from the card, which is what another program would
    find missing. Like the host number it only grows, so it needs one candidate per process.
    """
    try:
        import torch
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    mib = 1024 * 1024
    return (
        torch.cuda.max_memory_allocated(0) / mib,
        torch.cuda.max_memory_reserved(0) / mib,
    )


def _peak_memory_windows() -> float | None:  # pragma: no cover - exercised on the Windows runner
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        )

    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    )
    ok = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    return counters.PeakWorkingSetSize / (1024 * 1024) if ok else None
