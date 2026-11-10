"""Measure retrieval: ``python -m vault.eval [--corpus FOLDER] [--questions FILE] [--json FILE]``.

By default it runs the public eval set (``eval/public/questions.yaml``) against the synthetic demo
corpus (``demo/corpus/``, through its ``manifest.json``); ``--corpus`` and ``--questions`` point
it at another corpus and eval file in the same formats, such as the owner's own, kept outside the
repository. What it runs and how it scores is in :mod:`vault.retrieval.evaluation`.

The output is numbers and counts only, on the console and in the ``--json`` report: never a
question, an id, a path, a folder or document text, so a run on private files can be shown or
kept. Messages are fixed text, an error this package does not own is shown only by its class
name, a malformed command line is refused without repeating what was typed (``argparse`` would
quote it), and what libraries log or warn while it runs is not shown. ``--json`` names a file that
must not exist yet; the folder it is in is tried before the run starts.

The exit code is 0 when the run finished, whatever the scores, and not 0 when it could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from vault_spikes.__main__ import libraries_silenced
from vault_spikes.errors import OwnMessage

from vault.backup import FixedErrorParser
from vault.retrieval import evaluation
from vault.retrieval.evaluation import EvalError, Evaluation
from vault.storage.layout import VaultPathError

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
DEFAULT_CORPUS: Final = REPO_ROOT / "demo" / "corpus"
DEFAULT_QUESTIONS: Final = REPO_ROOT / "eval" / "public" / "questions.yaml"
EXIT_FAILED: Final = 1
EXIT_INTERRUPTED: Final = 130


def parser() -> argparse.ArgumentParser:
    command = FixedErrorParser(
        prog="python -m vault.eval",
        description="Score keyword search on an eval set: Recall@10, MRR and Hit@1.",
        allow_abbrev=False,
    )
    command.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        metavar="FOLDER",
        help="the corpus folder, with its manifest.json (default: demo/corpus)",
    )
    command.add_argument(
        "--questions",
        type=Path,
        default=DEFAULT_QUESTIONS,
        metavar="FILE",
        help="the eval file (default: eval/public/questions.yaml)",
    )
    command.add_argument(
        "--json", type=Path, metavar="FILE", help="also write the numbers to this new file"
    )
    return command


def _share(value: float | None) -> str:
    return "none scored" if value is None else f"{value:.3f}"


def _by_word(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{word} {count}" for word, count in sorted(counts.items())) or "0"


def report(result: Evaluation) -> list[str]:
    """The lines printed after a run: counts and scores, and words of this package only."""
    d, q, s = result.documents, result.questions, result.scores
    hit = (
        f"{_share(s.hit_at_1)} over {s.hit_at_1_questions} find-the-document questions"
        if s.hit_at_1 is not None
        else "no find-the-document question was scored"
    )
    return [
        f"Retrieval evaluation: {result.search} search, chunker {result.chunker}, "
        f"top {result.k}, one result per document, text fallback {result.text_fallback_encoding}",
        f"Documents listed: {d.listed}",
        f"  indexed: {d.indexed} ({d.chunks} chunks)",
        f"  exact duplicates of another listed file, stored once: {d.duplicates}",
        f"  not indexed, no extractor for the type yet: {_by_word(d.no_extractor)}",
        f"  not indexed, refused at import: {_by_word(d.refused)}",
        f"  not indexed, extraction failed: {d.extraction_failed}",
        f"Questions: {q.total}",
        f"  not answerable from the corpus (abstention, not scored here): {q.not_answerable}",
        f"  scored: {q.scored}",
        f"  not scored, no passage that counts is indexed: {q.none_indexed}",
        f"  not scored, a required passage is not indexed: {q.partly_indexed}",
        f"  queries search refused (scored as misses): {q.queries_refused}",
        f"  required passages no chunk holds (cannot be found): {q.passages_missing}",
        f"Recall@{result.k}: {_share(s.recall_at_10)}",
        f"MRR@{result.k}: {_share(s.mrr_at_10)}",
        f"Hit@1: {hit}",
    ]


def _check_destination(path: Path) -> None:
    """Refuse a ``--json`` file that exists (a link counts, even a dangling one) or whose folder
    cannot be written, before the run, so that a run is never thrown away at the end."""
    if os.path.lexists(path):
        msg = "the --json file already exists; move it or name another"
        raise EvalError(msg)
    try:
        with tempfile.TemporaryFile(dir=path.parent if str(path.parent) else "."):
            pass
    except OSError:
        msg = "the folder of the --json file is missing or cannot be written to"
        raise EvalError(msg) from None


def _write(path: Path, result: Evaluation) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:  # never replaces a file
            json.dump(result.as_json(), handle, indent=2)
            handle.write("\n")
    except FileExistsError:
        msg = "a file appeared under the --json name during the run; it was left alone"
        raise EvalError(msg) from None
    except OSError:
        msg = "the --json file could not be written"
        raise EvalError(msg) from None


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with libraries_silenced():
            if args.json is not None:
                _check_destination(args.json)
            result = evaluation.evaluate(args.corpus, args.questions)
            for line in report(result):
                print(line)
            if args.json is not None:
                _write(args.json, result)
    except (OwnMessage, VaultPathError) as error:
        # Fixed text of this package, the spikes' loaders or the vault layout (a temporary folder
        # inside a Git repository is refused); it names no file and quotes nothing.
        print(f"error: {error}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("error: the evaluation was interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as error:
        # An operating system's or a library's message can quote a private path or document text.
        print(
            f"error: the evaluation stopped with {type(error).__name__}; no further detail is "
            "printed because it could contain a file name or document text",
            file=sys.stderr,
        )
        return EXIT_FAILED
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
