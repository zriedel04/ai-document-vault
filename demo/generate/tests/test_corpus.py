"""Checks that the generated corpus and eval set are consistent with each other and with the rules.

Run from demo/generate: ``uv run python -m unittest discover -s tests -v``.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import re
import sys
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from PIL import Image

import build
from model import Document
from questions import build_questions

DOCS = {d.path: d for d in build.all_documents()}
FILES = build.build_outputs()
QUESTIONS = build_questions()
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def norm(text: str) -> str:
    return " ".join(text.split())


def data(doc: Document) -> bytes:
    return FILES[f"demo/corpus/{doc.path}"]


class CorpusTests(unittest.TestCase):
    def test_every_format_in_the_plan_is_present(self) -> None:
        kinds = {d.kind for d in DOCS.values()}
        self.assertEqual(kinds, {"pdf_text", "pdf_scan", "docx", "txt", "md", "csv", "png", "jpg"})

    def test_paths_are_relative_and_clean(self) -> None:
        for path in DOCS:
            self.assertFalse(path.startswith("/"), path)
            self.assertNotIn("..", path)
            self.assertEqual(path, path.lower(), "lower-case paths keep Windows and Linux in step")

    def test_exact_duplicates_are_byte_identical(self) -> None:
        found = 0
        for doc in DOCS.values():
            if doc.exact_duplicate_of:
                found += 1
                self.assertEqual(data(doc), data(DOCS[doc.exact_duplicate_of]), doc.path)
        self.assertGreaterEqual(found, 2)

    def test_no_other_documents_are_byte_identical(self) -> None:
        seen: dict[str, str] = {}
        for doc in DOCS.values():
            if doc.exact_duplicate_of:
                continue
            digest = hashlib.sha256(data(doc)).hexdigest()
            self.assertNotIn(digest, seen, f"{doc.path} is identical to {seen.get(digest)}")
            seen[digest] = doc.path

    def test_near_duplicates_are_similar_but_not_equal(self) -> None:
        found = 0
        for doc in DOCS.values():
            if doc.near_duplicate_of:
                found += 1
                other = DOCS[doc.near_duplicate_of]
                a = "\n".join(doc.page_texts()).splitlines()
                b = "\n".join(other.page_texts()).splitlines()
                self.assertNotEqual(a, b, doc.path)
                ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
                self.assertGreater(ratio, 0.5, f"{doc.path}: only {ratio:.2f} of the lines match")
        self.assertGreaterEqual(found, 2)

    def test_text_pdfs_have_a_text_layer_and_scans_do_not(self) -> None:
        for doc in DOCS.values():
            raw = data(doc)
            if doc.kind == "pdf_text":
                self.assertIn(b"Tj", raw, doc.path)
                self.assertNotIn(b"/DCTDecode", raw, doc.path)
                self.assertEqual(raw.count(b"/Type /Page\n"), len(doc.blocks), doc.path)
            if doc.kind == "pdf_scan":
                self.assertNotIn(b" Tj", raw, f"{doc.path} must have no text layer")
                self.assertEqual(raw.count(b"/DCTDecode"), len(doc.lines), doc.path)

    def test_pdf_text_matches_the_ground_truth(self) -> None:
        for doc in DOCS.values():
            if doc.kind != "pdf_text":
                continue
            raw = data(doc).decode("latin-1")
            for block in (b for page in doc.blocks for b in page):
                first_word = block.text.split()[0]
                self.assertIn(f"({first_word}", raw, f"{doc.path}: {block.text[:30]}")

    def test_docx_text_matches_the_ground_truth(self) -> None:
        for doc in DOCS.values():
            if doc.kind != "docx":
                continue
            with zipfile.ZipFile(io.BytesIO(data(doc))) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.namelist()[0], "[Content_Types].xml")
                for info in archive.infolist():
                    self.assertEqual(info.date_time, (2026, 1, 1, 0, 0, 0), "fixed timestamps")
                root = ET.fromstring(archive.read("word/document.xml"))  # noqa: S314 - our own output
            paragraphs = [
                "".join(t.text or "" for t in p.iter(f"{W}t")) for p in root.iter(f"{W}p")
            ]
            self.assertEqual("\n".join(paragraphs), doc.page_texts()[0], doc.path)

    def test_images_open_and_have_a_sensible_size(self) -> None:
        for doc in DOCS.values():
            if doc.kind in ("png", "jpg"):
                with Image.open(io.BytesIO(data(doc))) as image:
                    self.assertEqual(image.format, "PNG" if doc.kind == "png" else "JPEG")
                    self.assertGreater(image.width, 400, doc.path)
                    self.assertGreater(image.height, 200, doc.path)
                self.assertLess(len(data(doc)), 500_000, doc.path)

    def test_files_stay_under_the_repository_size_limit(self) -> None:
        for path, content in FILES.items():
            self.assertLess(len(content), 1_000_000, path)

    def test_only_fictional_email_addresses(self) -> None:
        for doc in DOCS.values():
            for address in re.findall(
                r"[\w.+-]+@[\w-]+(?:\.[A-Za-z][\w-]*)+", "\n".join(doc.page_texts())
            ):
                self.assertTrue(address.endswith(".example"), f"{doc.path}: {address}")

    def test_generation_is_deterministic(self) -> None:
        again = build.build_outputs()
        self.assertEqual(again, FILES)

    def test_manifest_matches_the_files(self) -> None:
        manifest = json.loads(FILES["demo/corpus/manifest.json"])
        listed = {e["path"]: e for e in manifest["documents"]}
        self.assertEqual(set(listed), set(DOCS))
        for path, entry in listed.items():
            self.assertEqual(
                entry["sha256"], hashlib.sha256(FILES[f"demo/corpus/{path}"]).hexdigest()
            )
            self.assertEqual(entry["bytes"], len(FILES[f"demo/corpus/{path}"]))


class EvalSetTests(unittest.TestCase):
    def test_size_and_ids(self) -> None:
        self.assertTrue(40 <= len(QUESTIONS) <= 60, len(QUESTIONS))
        ids = [q.id for q in QUESTIONS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len({q.question for q in QUESTIONS}), len(QUESTIONS), "unique questions")

    def test_every_category_is_covered(self) -> None:
        by_category: dict[str, int] = {}
        for q in QUESTIONS:
            by_category[q.category] = by_category.get(q.category, 0) + 1
        for category in ("lookup", "multi_doc", "version", "ocr", "numeric", "paraphrase"):
            self.assertGreaterEqual(by_category.get(category, 0), 3, category)
        self.assertGreaterEqual(by_category.get("not_in_vault", 0), 8)

    def test_every_source_quote_is_in_the_document(self) -> None:
        for q in QUESTIONS:
            for src in q.sources:
                doc = DOCS[src.path]
                texts = doc.page_texts()
                if doc.paginated:
                    self.assertIsNotNone(src.page, f"{q.id}: {src.path} needs a page")
                    assert src.page is not None
                    self.assertTrue(1 <= src.page <= len(texts), f"{q.id}: page {src.page}")
                    haystack = norm(texts[src.page - 1])
                else:
                    self.assertIsNone(src.page, f"{q.id}: {src.path} has no pages")
                    haystack = norm(texts[0])
                self.assertIn(norm(src.quote), haystack, f"{q.id}: {src.quote!r} in {src.path}")

    def test_answerable_questions_have_sources_and_others_have_none(self) -> None:
        for q in QUESTIONS:
            if q.answerable:
                self.assertTrue(q.sources, q.id)
            else:
                self.assertFalse(q.sources, q.id)

    def test_distractors_exist_and_are_not_also_sources(self) -> None:
        for q in QUESTIONS:
            for path in q.distractors:
                self.assertIn(path, DOCS, q.id)
            cited = {s.path for s in q.sources}
            self.assertFalse(cited & set(q.distractors), q.id)

    def test_version_questions_name_the_outdated_document(self) -> None:
        for q in QUESTIONS:
            if q.category == "version":
                self.assertTrue(q.distractors, q.id)

    def test_ocr_questions_are_only_answerable_from_pictures_and_scans(self) -> None:
        for q in QUESTIONS:
            if q.category == "ocr":
                for src in q.sources:
                    self.assertIn(DOCS[src.path].kind, ("pdf_scan", "png", "jpg"), q.id)

    def test_not_in_vault_questions_are_really_not_in_the_vault(self) -> None:
        corpus_text = norm(" ".join(" ".join(d.page_texts()) for d in DOCS.values())).lower()
        for term in ("phone", "enrolled", "midterm", "github", "jordan.lee@", "next year"):
            self.assertNotIn(term, corpus_text, term)

    def test_committed_yaml_matches_the_generator(self) -> None:
        self.assertEqual(
            yaml.safe_load(FILES["eval/public/questions.yaml"])["questions"],
            [build.question_entry(q) for q in QUESTIONS],
        )


if __name__ == "__main__":
    unittest.main()
