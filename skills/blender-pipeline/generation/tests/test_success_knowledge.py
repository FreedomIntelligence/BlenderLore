from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import blender_knowledge_common as kc
import build_blender_knowledge_index as builder
import retrieve_blender_knowledge as retrieval
import update_replay_knowledge_base as updater


class SuccessKnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / "knowledge"
        for field, path in {
            "KNOWLEDGE_ROOT": self.store,
            "MANIFEST_PATH": self.store / "blender_knowledge_manifest.jsonl",
            "QDRANT_PATH": self.store / "qdrant",
            "BUILDS_PATH": self.store / "builds",
            "CURRENT_BUILD_PATH": self.store / "current.json",
        }.items():
            patcher = mock.patch.object(kc, field, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(
            "os.environ",
            {
                "BLENDER_KNOWLEDGE_CANDIDATE_ROOT": str(
                    self.root / "knowledge_candidates"
                )
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def observation(
        self, *, status: str = "candidate", outcome: str = "qa_passed"
    ) -> kc.KnowledgeChunk:
        chunk = kc.make_chunk(
            self.root / "tutorial.md",
            "Keep verified material assignments",
            "Preserve the reviewed material assignment and silhouette. " * 4,
            source_type="run_artifact",
            extra={"outcome": outcome, "candidate_rule_id": "material-rule"},
        )
        chunk.review_status = status
        return chunk

    def evidence(self) -> dict:
        return {
            "scope": {
                "route": "static",
                "asset_family": "material_shader",
                "blender_version": "5.1.0",
            },
            "candidate_records": [
                {
                    "asset_id": f"asset-{index}",
                    "candidate_rule_id": "material-rule",
                    "review_status": "candidate",
                    "human_reviewed": True,
                    "outcome": "accepted",
                    "review_id": f"review-{index}",
                    "artifact_sha256": str(index) * 64,
                }
                for index in range(5)
            ],
            "accepted_holdout_records": [
                {
                    "asset_id": "holdout",
                    "baseline_status": "accepted",
                    "candidate_status": "accepted",
                    "quality_regression": False,
                    "human_reviewed": True,
                    "review_id": "holdout-review",
                    "artifact_sha256": "a" * 64,
                }
            ],
        }

    def test_bundled_guidance_is_useful_without_claiming_recipe_success(self) -> None:
        chunks = builder.build_chunks()
        self.assertTrue(chunks)
        self.assertTrue(
            all(kc.active_knowledge_eligible(chunk.payload()) for chunk in chunks)
        )
        self.assertEqual({chunk.review_status for chunk in chunks}, {"curated"})
        self.assertFalse(any(chunk.extra.get("recipe_id") for chunk in chunks))
        self.assertEqual(len(builder.collect_render_recipe_chunks()), 8)
        self.assertTrue(any("failure" in chunk.text for chunk in chunks))

    def test_manifest_only_build_and_retrieval_never_load_embedding_dependencies(
        self,
    ) -> None:
        with mock.patch.object(
            kc, "load_embedder", side_effect=AssertionError("no embedding")
        ):
            summary = builder.build_generation(
                builder.build_chunks(), manifest_only=True
            )
            pack = retrieval.build_pack(
                self.root,
                tutorial="Preserve material assignments and render the source camera.",
            )
        self.assertEqual(pack["status"], "ok")
        self.assertEqual(pack["retrieval_backend"], "manifest_lexical")
        self.assertTrue(pack["results"])
        self.assertEqual(pack["knowledge_generation"], summary["build_id"])
        self.assertEqual(pack["candidate_hits"], [])
        self.assertEqual(pack["deprecated_hits"], [])

    def test_raw_candidates_and_unattested_reviewed_rows_are_filtered(self) -> None:
        curated = builder.build_chunks()[0].payload()
        rows = [
            curated,
            self.observation().payload(),
            self.observation(outcome="qa_failed").payload(),
            self.observation(status="reviewed").payload(),
        ]
        kc.write_jsonl(rows)
        matches = retrieval.manifest_fallback_search(
            "material render pipeline failure", "material_shader", "other", 20
        )
        self.assertEqual([row["source_id"] for row in matches], [curated["source_id"]])
        with self.assertRaises(kc.KnowledgeManifestError):
            builder.build_generation([self.observation()], manifest_only=True)

    def test_alternate_backend_cannot_expose_failed_rows(self) -> None:
        curated = builder.build_chunks()[0].payload()
        with (
            mock.patch.object(kc, "active_qdrant_path", return_value=self.root),
            mock.patch.object(
                retrieval,
                "qdrant_search",
                return_value=[curated, self.observation(outcome="qa_failed").payload()],
            ),
        ):
            pack = retrieval.build_pack(self.root, tutorial="material render")
        self.assertEqual(len(pack["results"]), 1)
        self.assertEqual(pack["results"][0]["review_status"], "curated")

    def test_success_candidate_stays_outside_active_store(self) -> None:
        self.assertEqual(updater.append_unique([self.observation()]), 0)
        path = updater.candidate_manifest_path()
        self.assertNotIn(self.store, path.parents)
        self.assertEqual(len(kc.read_jsonl(path)), 1)
        self.assertEqual(kc.read_jsonl(), [])
        self.assertFalse(kc.CURRENT_BUILD_PATH.exists())

    def test_failed_or_missing_review_snapshot_produces_no_knowledge(self) -> None:
        for status in ("failed", "", "needs_review"):
            snapshot = {
                "schema": updater.KNOWLEDGE_SNAPSHOT_SCHEMA,
                "source_info": {},
                "pipeline_review": {"status": status},
                "linked_asset_knowledge": {},
                "documents": {"tutorial.md": "material recipe " * 30},
            }
            result = updater.update_from_knowledge_snapshot(
                snapshot, video_dir=self.root, updated_at="now"
            )
            self.assertEqual(result["chunk_count"], 0)
            self.assertFalse(updater.candidate_manifest_path().exists())
            self.assertEqual(kc.read_jsonl(), [])

    def test_admission_requires_success_review_scope_and_unrelated_holdout(
        self,
    ) -> None:
        evidence = self.evidence()
        guard = updater.candidate_promotion_guard(
            evidence["candidate_records"], evidence["accepted_holdout_records"]
        )
        self.assertTrue(guard["eligible_for_reviewed"])
        for mutation in ("overlap", "unreviewed", "unbound", "regression"):
            changed = copy.deepcopy(evidence)
            holdout = changed["accepted_holdout_records"][0]
            if mutation == "overlap":
                holdout["asset_id"] = "asset-0"
            elif mutation == "unreviewed":
                holdout["human_reviewed"] = False
            elif mutation == "unbound":
                holdout["artifact_sha256"] = ""
            else:
                holdout["quality_regression"] = True
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(updater.ReplayKnowledgePromotionError),
            ):
                updater.append_unique(
                    [self.observation(status="reviewed")], promotion_evidence=changed
                )
        with self.assertRaises(updater.ReplayKnowledgePromotionError):
            updater.append_unique(
                [self.observation(status="reviewed", outcome="qa_failed")],
                promotion_evidence=evidence,
            )
        evidence.pop("scope")
        with self.assertRaises(updater.ReplayKnowledgePromotionError):
            updater.append_unique(
                [self.observation(status="reviewed")], promotion_evidence=evidence
            )

    def test_admitted_success_activates_atomically_and_preserves_lexical_backend(
        self,
    ) -> None:
        with mock.patch.object(
            updater, "_rebuild_qdrant", side_effect=AssertionError("manifest only")
        ):
            self.assertEqual(
                updater.append_unique(
                    [self.observation(status="reviewed")],
                    promotion_evidence=self.evidence(),
                ),
                1,
            )
        rows = kc.read_jsonl()
        self.assertEqual(len(rows), 1)
        self.assertTrue(kc.active_knowledge_eligible(rows[0]))
        self.assertIsNone(kc.active_qdrant_path())
        scope_context = {
            "inferred_asset_family": "material_shader",
            "render_knowledge_context": {"route": "static", "blender_version": "5.1.0"},
        }
        self.assertTrue(retrieval.admitted_scope_matches(rows[0], scope_context))
        scope_context["render_knowledge_context"]["blender_version"] = "4.5.0"
        self.assertFalse(retrieval.admitted_scope_matches(rows[0], scope_context))
        rows[0]["text"] += " changed"
        self.assertFalse(kc.active_knowledge_eligible(rows[0]))

    def test_legacy_failed_rows_are_removed_on_update(self) -> None:
        curated = builder.build_chunks()[0].payload()
        kc.write_jsonl([curated, self.observation(outcome="qa_failed").payload()])
        self.assertEqual(updater.append_unique([]), 1)
        self.assertEqual(
            [row["source_id"] for row in kc.read_jsonl()], [curated["source_id"]]
        )

    def test_chunk_limits_preserve_heading_free_tails_and_long_paragraphs(self) -> None:
        text = "x" * 7100
        parts = kc.split_markdown_text(text, fallback_title="long", max_chars=2200)
        self.assertEqual("".join(body for _, body in parts), text)
        self.assertTrue(all(len(body) <= 2200 for _, body in parts))
        self.assertEqual(
            [title for title, _ in parts],
            ["long", "long part 2", "long part 3", "long part 4"],
        )
        sections = kc.split_markdown_text(
            "preamble\n\n# Rule\n\n" + text, fallback_title="source", max_chars=2200
        )
        self.assertEqual(sections[0], ("source", "preamble"))
        self.assertTrue(all(len(body) <= 2200 for _, body in sections))
        self.assertEqual(
            len(
                "".join(body for _, body in sections)
                .replace("preamble", "")
                .replace("# Rule", "")
                .strip()
            ),
            len(text),
        )

    def test_code_comments_are_not_headings_and_repeated_sections_have_unique_ids(
        self,
    ) -> None:
        sections = kc.split_markdown_text(
            "## Rule\n\n```python\n# code comment\nx = 1\n```\n\n## Rule\n\nSecond rule.",
            fallback_title="source",
        )
        self.assertEqual(
            [title for title, _ in sections], ["Rule", "Rule occurrence 2"]
        )
        self.assertIn("# code comment", sections[0][1])
        self.assertNotEqual(
            kc.make_chunk(self.root / "source.md", *sections[0]).source_id,
            kc.make_chunk(self.root / "source.md", *sections[1]).source_id,
        )

    def write_linked_source(
        self, *, audit_hash: str = "a" * 64, status: str = "pass"
    ) -> None:
        kc.atomic_write_json(
            self.root / "source.info.json",
            {
                "title": "Linked source",
                "source_kind": "video_replay_type2",
                "linked_source": {"selected_model_sha256": "a" * 64},
            },
        )
        kc.atomic_write_json(
            self.root / "linked_asset_audit.json",
            {
                "status": status,
                "selected_model_sha256": audit_hash,
                "source_scene": {"renderable_object_count": 1},
            },
        )

    def test_linked_source_projection_preserves_hash_without_admitting_a_recipe(
        self,
    ) -> None:
        self.write_linked_source()
        pack = retrieval.retrieve_for_pipeline(
            self.root, tutorial="Preserve the linked source materials."
        )
        projected = json.loads((self.root / "linked_asset_knowledge.json").read_text())
        self.assertEqual(projected["selected_model_sha256"], "a" * 64)
        self.assertEqual(projected["role"], "advisory_reproduction_evidence")
        self.assertEqual(projected, pack["context"]["linked_asset_knowledge"])
        self.assertIsNot(projected["recipe_match"]["executable"], True)
        self.assertEqual(kc.read_jsonl(), [])

    def test_linked_source_projection_rejects_mismatched_or_failed_audit(self) -> None:
        for hash_value, status in (
            ("b" * 64, "pass"),
            ("", "pass"),
            ("a" * 64, "blocked"),
        ):
            self.write_linked_source(audit_hash=hash_value, status=status)
            with (
                self.subTest(hash_value=hash_value, status=status),
                self.assertRaises(kc.KnowledgeManifestError),
            ):
                retrieval.retrieve_for_pipeline(
                    self.root, tutorial="Preserve the linked source."
                )
            self.assertFalse((self.root / "linked_asset_knowledge.json").exists())

    def test_linked_projection_never_overwrites_independent_data(self) -> None:
        self.write_linked_source()
        path = self.root / "linked_asset_knowledge.json"
        existing = {
            "selected_model_sha256": "a" * 64,
            "role": "advisory_reproduction_evidence",
            "independent_review": "retained",
        }
        kc.atomic_write_json(path, existing)
        before = path.read_bytes()
        retrieval.retrieve_for_pipeline(self.root, tutorial="Preserve the source.")
        self.assertEqual(path.read_bytes(), before)
        existing["selected_model_sha256"] = "b" * 64
        kc.atomic_write_json(path, existing)
        before = path.read_bytes()
        with self.assertRaises(kc.KnowledgeManifestError):
            retrieval.retrieve_for_pipeline(self.root, tutorial="Preserve the source.")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
