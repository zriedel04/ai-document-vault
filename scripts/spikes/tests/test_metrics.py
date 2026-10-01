from __future__ import annotations

import math
import sys
import unittest
from collections.abc import Callable

from vault_spikes import metrics


class EditDistanceTest(unittest.TestCase):
    def test_known_distances(self) -> None:
        self.assertEqual(metrics.edit_distance("kitten", "sitting"), 3)
        self.assertEqual(metrics.edit_distance("", "abc"), 3)
        self.assertEqual(metrics.edit_distance("abc", ""), 3)
        self.assertEqual(metrics.edit_distance("same", "same"), 0)
        self.assertEqual(metrics.edit_distance(["a", "b"], ["a", "c", "b"]), 1)

    def test_is_symmetric(self) -> None:
        self.assertEqual(
            metrics.edit_distance("flaw", "lawn"), metrics.edit_distance("lawn", "flaw")
        )


class ErrorRateTest(unittest.TestCase):
    def test_character_error_rate(self) -> None:
        self.assertEqual(metrics.character_error_rate("hello", "hello"), 0.0)
        self.assertAlmostEqual(metrics.character_error_rate("hello", "hallo"), 0.2)
        self.assertEqual(metrics.character_error_rate("abc", ""), 1.0)

    def test_whitespace_and_line_breaks_do_not_count(self) -> None:
        self.assertEqual(metrics.character_error_rate("a b\n c", "a   b c  "), 0.0)

    def test_an_empty_reference_cannot_be_scored(self) -> None:
        for reference in ("", "   \n"):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                metrics.character_error_rate(reference, "x")
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                metrics.word_error_rate(reference, "x")

    def test_word_error_rate(self) -> None:
        self.assertEqual(metrics.word_error_rate("the cat sat", "the cat sat"), 0.0)
        self.assertAlmostEqual(metrics.word_error_rate("the cat sat", "the dog sat"), 1 / 3)
        self.assertEqual(metrics.word_error_rate("one two", ""), 1.0)


class SummaryTest(unittest.TestCase):
    def test_percentiles(self) -> None:
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(metrics.percentile(data, 0), 1.0)
        self.assertEqual(metrics.percentile(data, 1), 5.0)
        self.assertEqual(metrics.median(data), 3.0)
        self.assertAlmostEqual(metrics.percentile(data, 0.9), 4.6)
        self.assertEqual(metrics.percentile([7.0], 0.95), 7.0)

    def test_bad_input_is_an_error(self) -> None:
        calls: list[Callable[[], object]] = [
            lambda: metrics.percentile([], 0.5),
            lambda: metrics.percentile([1.0], 1.5),
            lambda: metrics.mean([]),
            lambda: metrics.per_second(5, 0),
            lambda: metrics.cosine([1.0], [1.0, 2.0]),
        ]
        for call in calls:
            with self.assertRaises(ValueError):
                call()

    def test_mean_and_rate(self) -> None:
        self.assertEqual(metrics.mean([1.0, 2.0, 6.0]), 3.0)
        self.assertEqual(metrics.per_second(10, 4), 2.5)

    def test_cosine(self) -> None:
        self.assertAlmostEqual(metrics.cosine([1.0, 0.0], [1.0, 0.0]), 1.0)
        self.assertAlmostEqual(metrics.cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertAlmostEqual(metrics.cosine([1.0, 1.0], [-1.0, -1.0]), -1.0)
        self.assertEqual(metrics.cosine([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_peak_memory_is_a_positive_number(self) -> None:
        peak = metrics.peak_memory_mib()
        # Windows and Linux both report it; the Windows runner proves the ctypes call is right.
        if sys.platform == "win32" or sys.platform.startswith("linux"):
            self.assertIsNotNone(peak)
        self.assertTrue(peak is None or (math.isfinite(peak) and 5 < peak < 1_000_000))


if __name__ == "__main__":
    unittest.main()
