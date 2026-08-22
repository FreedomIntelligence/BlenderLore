from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = PACKAGE_ROOT / "knowledge"
SCRIPTS = PACKAGE_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_package as validator


RULE_FIELDS = (
    "**Applies when:**",
    "**Rule:**",
    "**Verify:**",
    "**Search terms:**",
)


class KnowledgeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            (KNOWLEDGE / "manifest.json").read_text(encoding="utf-8")
        )
        cls.documents = {
            row["path"]: (KNOWLEDGE / row["path"]).read_text(encoding="utf-8")
            for row in cls.manifest["documents"]
        }

    def test_manifest_covers_the_maintained_window_and_all_documents(self) -> None:
        self.assertEqual("2026-08-22", self.manifest["updated_through"])
        self.assertEqual(
            {"from": "2026-08-07", "through": "2026-08-22"},
            self.manifest["recent_extraction_window"],
        )
        self.assertFalse(validator.check_json_schemas(PACKAGE_ROOT))
        self.assertFalse(validator.check_links(PACKAGE_ROOT))

    def test_every_maintained_rule_is_structured_and_searchable(self) -> None:
        for name, text in self.documents.items():
            if name == "index.md":
                continue
            blocks = re.split(r"(?m)^## ", text)[1:]
            with self.subTest(document=name):
                self.assertGreaterEqual(len(blocks), 8)
            for block in blocks:
                title = block.splitlines()[0]
                with self.subTest(document=name, rule=title):
                    positions = [block.find(field) for field in RULE_FIELDS]
                    self.assertTrue(all(position >= 0 for position in positions))
                    self.assertEqual(positions, sorted(positions))

    def test_knowledge_has_no_internal_run_or_machine_markers(self) -> None:
        body = "\n".join(self.documents.values())
        forbidden = (
            re.compile(r"/(?:Users|Volumes)/"),
            re.compile(r"/F[0-9]{8,}/"),
            re.compile(r"\b(?:RW[123]|GEN-V[0-9]+|EDIT-[0-9]+|BVID|Type[12])\b"),
            re.compile(r"\b[0-9a-f]{40,64}\b", re.IGNORECASE),
            re.compile(r"\b[0-9]+\s+(?:tests?|checks?)\s+passed\b", re.IGNORECASE),
        )
        for pattern in forbidden:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(body))

    def test_core_historical_and_recent_decisions_are_retrievable(self) -> None:
        required_concepts = {
            "evidence-and-routing.md": (
                "Q-Gate",
                "final-reference",
                "subject-local motion",
                "source selection",
            ),
            "generation.md": (
                "operation fidelity",
                "stage isolation",
                "visual contract",
                "joint score",
                "canonical blank",
                "scene-linear",
                "eight deterministic",
                "at most three immutable attempts",
            ),
            "editing.md": (
                "mutation scope",
                "volume ratio",
                "material graph",
                "aesthetic non-regression",
                "GLB export",
            ),
            "rendering-and-dynamics.md": (
                "foreground luminance",
                "motion envelope",
                "simulation bake",
                "encoding quality",
            ),
            "operations-and-knowledge.md": (
                "idempotent API",
                "five distinct",
                "unrelated holdout",
                "atomic publish",
            ),
        }
        for name, concepts in required_concepts.items():
            body = self.documents[name].casefold()
            for concept in concepts:
                with self.subTest(document=name, concept=concept):
                    self.assertIn(concept.casefold(), body)


if __name__ == "__main__":
    unittest.main()
