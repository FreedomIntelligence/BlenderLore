from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import blender_knowledge_common as kc
import build_blender_knowledge_index as build_index
import render_knowledge as rk
import retrieve_blender_knowledge as retrieve
import run_video_replay_main as replay_main
import total_asset_render_knowledge as total_asset_knowledge
import update_replay_knowledge_base as update_replay


class RenderContextTests(unittest.TestCase):
    def test_subject_only_highlight_clip_triggers_overexposure_rescue(self) -> None:
        flags = total_asset_knowledge.rescue_flags([
            "iso quality: mean=0.41, white_clip=0.08, "
            "subject_luma_clip=0.31"
        ])
        self.assertTrue(flags["overexposed"])
        self.assertFalse(flags["dark"])

    def test_rounded_highlight_boundary_and_dynamic_peak_trigger_rescue(self) -> None:
        boundary = total_asset_knowledge.rescue_flags([
            "front quality: subject_luma_clip=0.150"
        ])
        peak = total_asset_knowledge.rescue_flags([
            "video highlight quality: subject_luma_clip=0.050, "
            "peak_subject_luma_clip=0.300"
        ])
        self.assertTrue(boundary["overexposed"])
        self.assertTrue(peak["overexposed"])

    def test_dark_background_does_not_brighten_an_already_bright_subject(self) -> None:
        flags = total_asset_knowledge.rescue_flags([
            "preview quality: mean=0.028, subject_mean=0.612, "
            "subject_coverage=0.0460, subject_bbox_coverage=0.2067, "
            "subject_edge_sides=0, subject_luma_clip=0.139"
        ])
        self.assertFalse(flags["dark"])
        self.assertFalse(flags["overexposed"])

    def test_obsolete_post_render_gpu_retry_signature_is_fail_closed(self) -> None:
        self.assertEqual(
            total_asset_knowledge.RULES[
                total_asset_knowledge.POST_RENDER_GPU_ATTESTATION_RACE_RULE
            ]["status"],
            "reviewed",
        )
        explicit = {
            "asset_id": "003595",
            "status": "failed",
            "render_batch": "0003",
            "failure_stage": "contract_validation",
            "failure_category": "remote_render_failure",
            "knowledge_version": (
                total_asset_knowledge
                .OBSOLETE_POST_RENDER_GPU_ATTESTATION_KNOWLEDGE_VERSION
            ),
            "error": (
                "Saved: '/tmp/total_asset_render/batch0003/003595/"
                "output/six_views/iso.png'\n"
                "TOTAL_ASSET_GPU_UUID_ATTESTATION_FAILED "
                "expected=gpu-11111111-1111-1111-1111-111111111111 "
                "observed=none probe=ok\nBlender quit"
            ),
        }
        expected = (
            total_asset_knowledge.POST_RENDER_GPU_ATTESTATION_RACE_RULE,
        )
        self.assertEqual(
            total_asset_knowledge
            .obsolete_post_render_gpu_attestation_retry_rules(explicit),
            expected,
        )

        ending = (
            "/tmp/total_asset_render/batch0004/004023/"
            "output/six_views/iso.png\nBlender quit"
        )
        hidden = {
            **explicit,
            "asset_id": "004023",
            "render_batch": "0004",
            "failure_code": "RuntimeError",
            "assignment_mode": "dynamic_compatible_v1",
            "graphics_environment_policy": (
                total_asset_knowledge.OBSOLETE_NATIVE_CYCLES_POLICY
            ),
            "physical_worker": {
                "worker_index": 4,
                "remote_port": 30773,
                "gpu": 0,
            },
            "worker": "total_asset_render_status_batch0004_p30773_g0",
            "error": "x" * (3000 - len(ending)) + ending,
        }
        self.assertEqual(
            total_asset_knowledge
            .obsolete_post_render_gpu_attestation_retry_rules(hidden),
            expected,
        )

        rejected = (
            {**explicit, "status": "needs_review"},
            {**explicit, "failure_stage": "primary_render"},
            {**explicit, "knowledge_version": total_asset_knowledge.KNOWLEDGE_VERSION},
            {**explicit, "failure_category": "source_corrupt"},
            {**explicit, "error": explicit["error"].replace("observed=none", "observed=gpu-other")},
            {**explicit, "error": explicit["error"].replace("probe=ok", "probe=nvidia_smi_exit_1")},
            {**explicit, "error": explicit["error"].replace("six_views/iso.png", "six_views/top.png")},
            {**hidden, "graphics_environment_policy": "unreviewed"},
            {**hidden, "physical_worker": {"worker_index": 4, "remote_port": 30773, "gpu": 1}},
            {**hidden, "error": hidden["error"] + "x"},
            {**hidden, "error": hidden["error"][:-9] + "Traceback"},
        )
        for row in rejected:
            with self.subTest(row=row):
                self.assertEqual(
                    total_asset_knowledge
                    .obsolete_post_render_gpu_attestation_retry_rules(row),
                    (),
                )

    def test_obsolete_blender_loader_retry_signature_is_fail_closed(self) -> None:
        self.assertEqual(
            total_asset_knowledge.RULES[
                total_asset_knowledge.BLENDER_SHARED_LIBRARY_LOADER_RETRY_RULE
            ]["status"],
            "reviewed",
        )
        error = (
            "exitstatus=127 worker_remote_command_failed\n"
            "[remote stderr]\n"
            "/root/blender-4.5.10-linux-x64/blender: error while loading "
            "shared libraries: libSM.so.6: cannot open shared object file: "
            "No such file or directory"
        )
        previous = {
            "asset_id": "asset-0001",
            "identity_key": "identity-0001",
            "cycle_id": "cycle72-test",
            "lease_id": "lease-test",
            "workload_kind": "highqal_source",
            "status": "failed",
            "render_batch": "0000",
            "failure_stage": "primary_render",
            "failure_category": "remote_render_failure",
            "failure_code": "worker_remote_command_failed",
            "remote_exit_status": 127,
            "knowledge_version": (
                total_asset_knowledge
                .OBSOLETE_BLENDER_SHARED_LIBRARY_LOADER_KNOWLEDGE_VERSION
            ),
            "knowledge_generation": (
                total_asset_knowledge
                .OBSOLETE_BLENDER_SHARED_LIBRARY_LOADER_KNOWLEDGE_VERSION
            ),
            "assignment_mode": "dynamic_compatible_v1",
            "physical_worker": {
                "worker_index": 0,
                "remote_port": 31722,
                "gpu": 0,
                "node_boot_id": "boot-31722-before-libsm-repair",
            },
            "error": error,
        }
        expected = (
            total_asset_knowledge.BLENDER_SHARED_LIBRARY_LOADER_RETRY_RULE,
        )
        self.assertEqual(
            total_asset_knowledge
            .obsolete_post_render_gpu_attestation_retry_rules(previous),
            expected,
        )
        self.assertEqual(
            total_asset_knowledge
            .obsolete_post_render_gpu_attestation_retry_rules({
                **previous,
                "physical_worker": {
                    **previous["physical_worker"],
                    "worker_index": 3,
                    "gpu": 3,
                },
                "error": error.replace("libSM.so.6", "libICE.so.6"),
            }),
            expected,
        )

        rejected = (
            {**previous, "status": "needs_review"},
            {**previous, "failure_stage": "source_preflight"},
            {**previous, "failure_category": "missing_dependency"},
            {**previous, "failure_code": "RuntimeError"},
            {**previous, "remote_exit_status": "127"},
            {**previous, "knowledge_version": total_asset_knowledge.KNOWLEDGE_VERSION},
            {**previous, "knowledge_generation": total_asset_knowledge.KNOWLEDGE_VERSION},
            {**previous, "assignment_mode": "fixed_partition_v1"},
            {**previous, "identity_key": ""},
            {**previous, "cycle_id": ""},
            {**previous, "lease_id": ""},
            {**previous, "workload_kind": "unreviewed"},
            {**previous, "physical_worker": {**previous["physical_worker"], "remote_port": 30773}},
            {**previous, "physical_worker": {**previous["physical_worker"], "node_boot_id": ""}},
            {**previous, "physical_worker": {**previous["physical_worker"], "gpu": 1}},
            {**previous, "error": error.replace("exitstatus=127", "exitstatus=1")},
            {**previous, "error": error.replace("No such file or directory", "Permission denied")},
            {**previous, "error": "source texture libSM.so.6 is missing"},
            {
                **previous,
                "error": (
                    "exitstatus=127 worker_remote_command_failed\n"
                    "asset note: error while loading shared libraries: "
                    "not-a-library: cannot open shared object file: "
                    "No such file or directory"
                ),
            },
        )
        for row in rejected:
            with self.subTest(row=row):
                self.assertEqual(
                    total_asset_knowledge
                    .obsolete_post_render_gpu_attestation_retry_rules(row),
                    (),
                )

    def test_latin_boundaries_do_not_match_hair_inside_chair(self) -> None:
        self.assertEqual(kc.infer_asset_family("a wooden chair model"), "other")
        context = rk.build_render_knowledge_context("video_replay", title="wooden chair model")
        self.assertEqual(context.subject_family, "interior_furniture")
        self.assertNotIn("hair_fur", context.geometry_traits)

    def test_camera_model_is_not_motion(self) -> None:
        self.assertNotEqual(kc.infer_asset_family("camera model"), "motion")
        context = rk.build_render_knowledge_context("video_replay", title="camera model")
        self.assertEqual(context.subject_family, "electronics")
        self.assertEqual(context.route, "static")
        self.assertEqual(context.motion_mechanisms, ["none"])

    def test_cloth_material_is_not_cloth_simulation(self) -> None:
        context = rk.build_render_knowledge_context(
            "video_replay",
            title="布料材质 Cloth Material",
            verified_steps={"steps": [{"action": "设置布料材质和粗糙度"}]},
        )
        self.assertEqual(context.route, "static")
        self.assertEqual(context.subject_family, "material")
        self.assertIn("fabric", context.material_traits)
        self.assertNotIn("cloth_softbody", context.motion_mechanisms)

    def test_auto_rig_pro_preset_helper_paths_are_exact(self) -> None:
        for preset_directory in (
            "armature_presets",
            "limb_presets",
            "misc_presets",
        ):
            with self.subTest(preset_directory=preset_directory):
                self.assertTrue(total_asset_knowledge.is_helper_path(
                    Path("bundle/Auto-Rig Pro 3.75.14/auto_rig_pro-master")
                    / preset_directory
                    / "preset.blend"
                ))
        self.assertFalse(total_asset_knowledge.is_helper_path(
            Path("artist-project/armature_presets/hero.blend")
        ))
        self.assertFalse(total_asset_knowledge.is_helper_path(
            Path("artist-project/auto_rig_pro_final_character.blend")
        ))

    def test_cloth_simulation_is_only_a_candidate_without_scene_audit(self) -> None:
        context = rk.build_render_knowledge_context(
            "video_replay",
            verified_steps={"steps": [{"action": "运行 Cloth Simulation 并烘焙"}]},
        )
        self.assertEqual(context.route, "dynamic_candidate")
        self.assertIn("cloth_softbody", context.motion_mechanisms)
        self.assertFalse(rk.match_default_recipe(context).executable)

    def test_camera_only_and_armature_only_do_not_count_as_dynamic(self) -> None:
        camera = rk.build_render_knowledge_context(
            "total_asset",
            scene_audit={"animated_cameras": 1, "armature_objects": 1, "renderable": True},
        )
        self.assertEqual(camera.route, "static")
        self.assertEqual(camera.motion_mechanisms, ["camera_only"])
        armature = rk.build_render_knowledge_context(
            "total_asset",
            scene_audit={"armature_objects": 1, "renderable": True},
        )
        self.assertEqual(armature.route, "static")
        self.assertEqual(armature.motion_mechanisms, ["none"])

    def test_task1_emitted_camera_and_generic_driver_facts_stay_static(self) -> None:
        camera, _ = rk.bili_linked_asset_knowledge_decision(
            {"标题": "camera animation"},
            {
                "blender_version": "4.3.0",
                "source_scene": {
                    "renderable_object_count": 1,
                    "camera_count": 1,
                    "light_count": 1,
                    "animated_object_count": 0,
                    "animated_cameras": ["Camera"],
                    "driver_count": 0,
                    "time_dependent_drivers": 0,
                },
            },
        )
        generic_driver, _ = rk.bili_linked_asset_knowledge_decision(
            {"标题": "generic driven model"},
            {
                "source_scene": {
                    "renderable_object_count": 1,
                    "camera_count": 0,
                    "light_count": 0,
                    "animated_object_count": 0,
                    "animated_cameras": [],
                    "driver_count": 3,
                    "time_dependent_drivers": 0,
                },
            },
        )
        self.assertEqual(camera.route, "static")
        self.assertEqual(camera.motion_mechanisms, ["camera_only"])
        self.assertEqual(generic_driver.route, "static")
        self.assertEqual(generic_driver.motion_mechanisms, ["none"])

    def test_legacy_collapsed_motion_count_is_uncertain_not_object_transform(self) -> None:
        context = rk.build_render_knowledge_context(
            "bili_linked_asset",
            scene_audit={
                "renderable": True,
                "animated_object_count": 1,
            },
        )
        self.assertEqual(context.route, "dynamic_candidate")
        self.assertEqual(context.motion_mechanisms, ["unverified_metadata"])
        self.assertNotIn("object_transform", context.motion_mechanisms)

        explicit = rk.build_render_knowledge_context(
            "bili_linked_asset",
            scene_audit={
                "renderable": True,
                "animated_object_count": 2,
                "bone_animated_armatures": ["Rig"],
                "animated_shape_keys": ["Face"],
            },
        )
        self.assertEqual(explicit.route, "dynamic")
        self.assertEqual(set(explicit.motion_mechanisms), {"rig_pose", "shape_key"})
        self.assertNotIn("object_transform", explicit.motion_mechanisms)

    def test_three_entry_adapters_agree_on_motion_exclusion_boundaries(self) -> None:
        total_camera, _ = rk.total_asset_knowledge_decision(
            {"title": "camera animation"},
            {"renderable": True, "animated_cameras": ["Camera"]},
        )
        task1_camera, _ = rk.bili_linked_asset_knowledge_decision(
            {"标题": "camera animation"},
            {
                "source_scene": {
                    "renderable_object_count": 1,
                    "animated_object_count": 0,
                    "animated_cameras": ["Camera"],
                }
            },
        )
        replay_camera, _ = rk.video_replay_knowledge_decision(
            title="camera animation",
            tutorial="",
            verified_steps={"steps": ["camera animation"]},
        )
        for context in (total_camera, task1_camera, replay_camera):
            self.assertEqual(context.route, "static")
            self.assertEqual(context.motion_mechanisms, ["camera_only"])

        total_driver, _ = rk.total_asset_knowledge_decision(
            {"title": "generic driven model"},
            {"renderable": True, "drivers": 3},
        )
        task1_driver, _ = rk.bili_linked_asset_knowledge_decision(
            {"标题": "generic driven model"},
            {
                "source_scene": {
                    "renderable_object_count": 1,
                    "animated_object_count": 0,
                    "driver_count": 3,
                    "time_dependent_drivers": 0,
                }
            },
        )
        replay_driver, _ = rk.video_replay_knowledge_decision(
            title="generic driven model",
            tutorial="",
            verified_steps={"steps": ["configure a generic driver"]},
        )
        for context in (total_driver, task1_driver, replay_driver):
            self.assertEqual(context.route, "static")
            self.assertEqual(context.motion_mechanisms, ["none"])

    def test_generic_driver_is_static_but_time_driver_is_dynamic(self) -> None:
        generic = rk.build_render_knowledge_context(
            "total_asset", scene_audit={"drivers": 3, "renderable": True}
        )
        timed = rk.build_render_knowledge_context(
            "total_asset", scene_audit={"time_dependent_drivers": 1, "renderable": True}
        )
        self.assertEqual(generic.route, "static")
        self.assertEqual(generic.motion_mechanisms, ["none"])
        self.assertEqual(timed.route, "dynamic")
        self.assertIn("time_driver", timed.motion_mechanisms)

    def test_scene_audit_recognizes_supported_dynamic_mechanisms(self) -> None:
        cases = {
            "actions": "action_nla",
            "bone_animated_armatures": "rig_pose",
            "animated_shape_keys": "shape_key",
            "animated_materials": "material_animation",
            "geometry_nodes_time": "geometry_nodes_time",
            "hair_particle_systems": "particle_hair",
            "animated_particle_systems": "particle_emitter",
            "cloth_simulations": "cloth_softbody",
            "rigid_body_simulations": "rigid_body",
            "liquid_simulations": "fluid_liquid",
            "smoke_simulations": "smoke_fire",
        }
        for key, expected in cases.items():
            with self.subTest(key=key):
                context = rk.build_render_knowledge_context(
                    "total_asset", scene_audit={key: 1, "renderable": True}
                )
                self.assertEqual(context.route, "dynamic")
                self.assertIn(expected, context.motion_mechanisms)

    def test_scene_audit_has_higher_priority_than_dynamic_metadata(self) -> None:
        context = rk.build_render_knowledge_context(
            "total_asset",
            scene_audit={"renderable": True, "armature_objects": 1},
            catalog={"render_route": "dynamic_candidate", "technical_tags": "animation"},
            title="动画角色",
        )
        self.assertEqual(context.route, "static")
        self.assertEqual(context.evidence[0]["source"], "scene_audit")

    def test_positive_scene_audit_overrides_catalog_not_renderable(self) -> None:
        context = rk.build_render_knowledge_context(
            "total_asset",
            scene_audit={"renderable": True},
            catalog={"render_route": "not_renderable"},
            title="product asset",
        )
        self.assertEqual(context.route, "static")
        self.assertEqual(context.confidence, 0.82)

    def test_audit_error_is_not_deterministic_evidence(self) -> None:
        context = rk.build_render_knowledge_context(
            "total_asset",
            scene_audit={"audit_error": "timeout while opening file"},
            catalog={"render_route": "static"},
            title="product asset",
        )
        self.assertEqual(context.route, "static")
        self.assertEqual(context.confidence, 0.68)
        self.assertNotIn("scene_audit", [item["source"] for item in context.evidence])

    def test_empty_total_asset_contract_does_not_gain_audit_confidence(self) -> None:
        context, match = rk.total_asset_knowledge_decision({}, {})
        self.assertEqual(context.route, "unknown")
        self.assertEqual(context.confidence, 0.25)
        self.assertFalse(match.executable)

    def test_total_asset_adapter_uses_contract_animation_evidence(self) -> None:
        context, match = rk.total_asset_knowledge_decision(
            {"title": "liquid splash", "render_route": "dynamic_candidate"},
            {
                "render_route": "dynamic",
                "render_engine": "CYCLES",
                "animation_evidence": {"simulations": ["Water:FLUID"]},
            },
        )
        self.assertEqual(context.route, "dynamic")
        self.assertIn("fluid_liquid", context.motion_mechanisms)
        self.assertEqual(context.render_profile, "simulation_dynamic")
        self.assertFalse(match.executable)
        self.assertIn("dynamic_simulation", match.candidate_recipe_ids)

    def test_bili_adapter_preserves_source_and_ignores_unanimated_armature(self) -> None:
        context, match = rk.bili_linked_asset_knowledge_decision(
            {"标题": "角色模型"},
            {
                "source_scene": {
                    "renderable_object_count": 2,
                    "camera_count": 1,
                    "light_count": 1,
                    "armature_count": 1,
                    "animated_object_count": 0,
                    "render_engine": "BLENDER_EEVEE_NEXT",
                }
            },
        )
        self.assertEqual(context.route, "static")
        self.assertEqual(context.motion_mechanisms, ["none"])
        self.assertIn("preserve_source", context.runtime_traits)
        self.assertIn("source_camera", context.runtime_traits)
        self.assertFalse(match.executable)
        self.assertIn(
            "presentation_profile_mismatch",
            match.blocked_recipes["static_source_camera"],
        )

    def test_unknown_schema_is_rejected(self) -> None:
        with self.assertRaises(rk.RenderKnowledgeError):
            rk.RenderKnowledgeContext.from_dict(
                {"schema_version": "render-knowledge-context-v99", "source_kind": "total_asset"}
            )


class RecipePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = rk.build_render_knowledge_context(
            "total_asset",
            scene_audit={"renderable": True, "object_count": 1},
            title="product asset",
        )

    def recipe(self, recipe_id: str, status: str, **overrides) -> rk.RecipeSpec:
        values = {
            "recipe_id": recipe_id,
            "version": "1.0.0",
            "review_status": status,
            "source_kinds": ["total_asset"],
            "routes": ["static"],
            "render_profiles": ["six_view"],
            "validated_assets": 5,
            "validated_source_kinds": ["total_asset"],
            "human_reviewed": True,
        }
        values.update(overrides)
        return rk.RecipeSpec(**values)

    def approval(
        self,
        recipe: rk.RecipeSpec,
        *,
        validated_assets: int = 5,
        source_kinds: list[str] | None = None,
    ) -> rk.RecipeApproval:
        source_kinds = source_kinds or ["total_asset"]
        records = [
            {
                "asset_id": f"{source_kinds[index % len(source_kinds)]}-asset-{index:03d}",
                "source_kind": source_kinds[index % len(source_kinds)],
                "holdout": True,
                "human_reviewed": True,
                "reviewer_identity": "render-knowledge-maintainer",
                "outcome": "pass",
                "rare_dynamic": False,
            }
            for index in range(validated_assets)
        ]
        return rk.RecipeApproval(
            approval_id=f"approval:{recipe.recipe_id}:1",
            recipe_id=recipe.recipe_id,
            recipe_version=recipe.version,
            reviewer_identity="render-knowledge-maintainer",
            approved_at="2026-07-16T20:00:00+08:00",
            evidence_digest=rk.approval_evidence_digest(records),
            validated_assets=validated_assets,
            validated_source_kinds=source_kinds,
            evidence_records=records,
        )

    def test_candidate_is_advisory_and_deprecated_is_never_executable(self) -> None:
        candidate = self.recipe("candidate_recipe", "candidate")
        deprecated = self.recipe("old_recipe", "deprecated")
        match = rk.match_recipe(self.context, [candidate, deprecated])
        self.assertFalse(match.executable)
        self.assertEqual(match.candidate_recipe_ids, ["candidate_recipe"])
        self.assertEqual(match.deprecated_recipe_ids, ["old_recipe"])

    def test_reviewed_recipe_cannot_self_assert_promotion(self) -> None:
        unreviewed = self.recipe(
            "self_asserted_review",
            "reviewed",
            human_reviewed=True,
            validated_assets=999,
            validated_source_kinds=["total_asset"],
        )
        match = rk.match_recipe(self.context, [unreviewed])
        self.assertFalse(match.executable)
        self.assertIn(
            "approval_provenance_required",
            match.blocked_recipes["self_asserted_review"],
        )

    def test_reviewed_compatible_recipe_executes(self) -> None:
        reviewed = self.recipe("reviewed_recipe", "reviewed")
        approval = self.approval(reviewed)
        match = rk.match_recipe(
            self.context,
            [reviewed],
            approvals={approval.registry_key: approval},
        )
        self.assertTrue(match.executable)
        self.assertEqual(match.recipe_id, "reviewed_recipe")
        self.assertEqual(match.approval_id, approval.approval_id)

    def test_approval_evidence_must_cover_recipe_source_scope(self) -> None:
        reviewed = self.recipe("wrong_scope_recipe", "reviewed")
        approval = self.approval(reviewed, source_kinds=["video_replay"])
        match = rk.match_recipe(
            self.context,
            [reviewed],
            approvals={approval.registry_key: approval},
        )
        self.assertFalse(match.executable)
        self.assertIn(
            "approval_source_scope_mismatch",
            match.blocked_recipes[reviewed.recipe_id],
        )

    def test_legacy_scalar_approval_claims_are_not_trusted(self) -> None:
        reviewed = self.recipe("legacy_scalar", "reviewed")
        legacy = rk.RecipeApproval(
            approval_id="legacy:1",
            recipe_id=reviewed.recipe_id,
            recipe_version=reviewed.version,
            reviewer_identity="legacy-reviewer",
            approved_at="2026-07-16T20:00:00+08:00",
            evidence_digest="a" * 64,
            validated_assets=999,
            validated_source_kinds=["total_asset"],
        )
        match = rk.match_recipe(
            self.context,
            [reviewed],
            approvals={legacy.registry_key: legacy},
        )
        self.assertFalse(match.executable)
        self.assertEqual(legacy.validated_assets, 0)
        self.assertIn(
            "approval_evidence_records_required",
            match.blocked_recipes[reviewed.recipe_id],
        )

    def test_approval_digest_and_unique_asset_records_are_enforced(self) -> None:
        record = {
            "asset_id": "asset-001",
            "source_kind": "total_asset",
            "holdout": True,
            "human_reviewed": True,
            "reviewer_identity": "reviewer",
        }
        common = {
            "approval_id": "approval:bad:1",
            "recipe_id": "reviewed_recipe",
            "recipe_version": "1.0.0",
            "reviewer_identity": "reviewer",
            "approved_at": "2026-07-16T20:00:00+08:00",
        }
        with self.assertRaisesRegex(rk.RenderKnowledgeError, "does not match"):
            rk.RecipeApproval(
                **common,
                evidence_digest="b" * 64,
                evidence_records=[record],
            )
        duplicate_records = [record, dict(record)]
        with self.assertRaisesRegex(rk.RenderKnowledgeError, "must be unique"):
            rk.RecipeApproval(
                **common,
                evidence_digest=rk.approval_evidence_digest(duplicate_records),
                evidence_records=duplicate_records,
            )

    def test_cross_pipeline_requires_twenty_actual_holdout_records(self) -> None:
        reviewed = self.recipe(
            "cross_pipeline",
            "reviewed",
            source_kinds=["total_asset", "video_replay"],
        )
        insufficient = self.approval(
            reviewed,
            validated_assets=19,
            source_kinds=["total_asset", "video_replay"],
        )
        blocked = rk.match_recipe(
            self.context,
            [reviewed],
            approvals={insufficient.registry_key: insufficient},
        )
        self.assertIn(
            "cross_pipeline_requires_20_holdout_assets",
            blocked.blocked_recipes[reviewed.recipe_id],
        )
        approved = self.approval(
            reviewed,
            validated_assets=20,
            source_kinds=["total_asset", "video_replay"],
        )
        match = rk.match_recipe(
            self.context,
            [reviewed],
            approvals={approved.registry_key: approved},
        )
        self.assertTrue(match.executable)

    def test_rare_dynamic_requires_five_flagged_human_review_records(self) -> None:
        reviewed = self.recipe("rare_dynamic", "reviewed", rare_dynamic=True)
        records = [
            {
                "asset_id": f"rare-{index}",
                "source_kind": "total_asset",
                "holdout": True,
                "human_reviewed": True,
                "reviewer_identity": "rare-reviewer",
                "outcome": "pass",
                "rare_dynamic": index < 4,
            }
            for index in range(5)
        ]
        approval = rk.RecipeApproval(
            approval_id="approval:rare:1",
            recipe_id=reviewed.recipe_id,
            recipe_version=reviewed.version,
            reviewer_identity="rare-reviewer",
            approved_at="2026-07-16T20:00:00+08:00",
            evidence_digest=rk.approval_evidence_digest(records),
            evidence_records=records,
        )
        match = rk.match_recipe(
            self.context,
            [reviewed],
            approvals={approval.registry_key: approval},
        )
        self.assertIn(
            "rare_dynamic_requires_five_reviewed_assets",
            match.blocked_recipes[reviewed.recipe_id],
        )
        bad_human = dict(records[0], human_reviewed=False)
        with self.assertRaisesRegex(rk.RenderKnowledgeError, "identified human review"):
            rk.approval_evidence_digest([bad_human])

    def test_rank_prefers_specificity_before_declared_priority(self) -> None:
        generic = self.recipe("generic", "reviewed", priority=999)
        specific = self.recipe(
            "specific",
            "reviewed",
            priority=0,
            required_traits=["subject:daily_object"],
        )
        approvals = {
            approval.registry_key: approval
            for approval in [self.approval(generic), self.approval(specific)]
        }
        match = rk.match_recipe(self.context, [generic, specific], approvals=approvals)
        self.assertEqual(match.recipe_id, "specific")

    def test_rank_prefers_larger_approval_holdout(self) -> None:
        less_validated = self.recipe("a_less_validated", "reviewed", priority=0)
        more_validated = self.recipe("z_more_validated", "reviewed", priority=0)
        less_approval = self.approval(less_validated, validated_assets=5)
        more_approval = self.approval(more_validated, validated_assets=50)
        match = rk.match_recipe(
            self.context,
            [less_validated, more_validated],
            approvals={
                less_approval.registry_key: less_approval,
                more_approval.registry_key: more_approval,
            },
        )
        self.assertEqual(match.recipe_id, "z_more_validated")

    def test_rank_prefers_newer_semantic_version(self) -> None:
        older = self.recipe("versioned", "reviewed", version="1.9.0")
        newer = self.recipe("versioned", "reviewed", version="1.10.0")
        older_approval = self.approval(older)
        newer_approval = self.approval(newer)
        match = rk.match_recipe(
            self.context,
            [older, newer],
            approvals={
                older_approval.registry_key: older_approval,
                newer_approval.registry_key: newer_approval,
            },
        )
        self.assertEqual(match.recipe_version, "1.10.0")

    def test_rank_uses_stable_recipe_id_as_final_tie_break(self) -> None:
        zeta = self.recipe("zeta", "reviewed", priority=0)
        alpha = self.recipe("alpha", "reviewed", priority=0)
        approvals = {
            approval.registry_key: approval
            for approval in [self.approval(zeta), self.approval(alpha)]
        }
        match = rk.match_recipe(self.context, [zeta, alpha], approvals=approvals)
        self.assertEqual(match.recipe_id, "alpha")

    def test_evidence_strength_orders_deterministic_audit_above_title(self) -> None:
        title_only = rk.build_render_knowledge_context(
            "total_asset",
            title="product asset",
        )
        audited = rk.build_render_knowledge_context(
            "total_asset",
            scene_audit={"renderable": True, "content_category": "product"},
            title="product asset",
        )
        self.assertGreater(
            rk._context_evidence_strength(audited),
            rk._context_evidence_strength(title_only),
        )

    def test_runtime_constraints_fail_closed_when_facts_are_unknown(self) -> None:
        recipe = self.recipe(
            "constrained_recipe",
            "reviewed",
            engines=["cycles"],
            min_blender_version="4.0",
            presentation_profile="studio_turntable",
        )
        match = rk.match_recipe(self.context, [recipe])
        self.assertFalse(match.executable)
        self.assertIn("engine_unknown", match.blocked_recipes[recipe.recipe_id])
        self.assertIn("blender_version_unknown", match.blocked_recipes[recipe.recipe_id])

    def test_unknown_presentation_fails_closed(self) -> None:
        context = rk.RenderKnowledgeContext(
            source_kind="total_asset",
            route="static",
            render_profile="six_view",
            subject_family="daily_object",
            presentation_profile="unknown",
            confidence=0.9,
        )
        recipe = self.recipe(
            "presentation_constrained",
            "reviewed",
            presentation_profile="studio_turntable",
        )
        match = rk.match_recipe(context, [recipe])
        self.assertIn("presentation_profile_unknown", match.blocked_recipes[recipe.recipe_id])

    def test_engine_and_version_are_hard_filters(self) -> None:
        context = rk.build_render_knowledge_context(
            "total_asset",
            scene_audit={"renderable": True, "engine": "CYCLES", "blender_version": "3.6"},
        )
        recipe = self.recipe(
            "eevee_blender4",
            "reviewed",
            engines=["eevee"],
            min_blender_version="4.0",
        )
        match = rk.match_recipe(context, [recipe])
        self.assertFalse(match.executable)
        self.assertIn("engine_mismatch", match.blocked_recipes[recipe.recipe_id])
        self.assertIn("blender_version_too_old", match.blocked_recipes[recipe.recipe_id])

    def test_duplicate_recipe_id_and_version_is_rejected(self) -> None:
        recipe = self.recipe("same", "reviewed")
        with self.assertRaises(rk.RenderKnowledgeError):
            rk.match_recipe(self.context, [recipe, recipe])


class KnowledgeChunkAndIndexTests(unittest.TestCase):
    def test_paper_collection_falls_back_to_outer_workspace_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            project = workspace / "video2blender"
            paper = (
                workspace
                / "paper/blender/paper-summaries/example/summary.zh.md"
            )
            project.mkdir(parents=True)
            paper.parent.mkdir(parents=True)
            paper.write_text(
                "# Verified Paper\n\n" + "evidence-backed Blender guidance " * 8,
                encoding="utf-8",
            )
            with (
                mock.patch.object(kc, "PROJECT", project),
                mock.patch.dict(os.environ, {}, clear=False),
            ):
                os.environ.pop("BLENDER_KNOWLEDGE_PAPER_ROOT", None)
                chunks = build_index.collect_paper_chunks()

            self.assertTrue(chunks)
            self.assertTrue(all(str(workspace / "paper") in row.source_path for row in chunks))

    def test_run_artifacts_follow_configured_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp) / "external-output"
            artifact = output_root / "blender_tutorial_replay/run-1/pipeline_summary.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                json.dumps({"status": "accepted", "details": "x" * 120}),
                encoding="utf-8",
            )
            with mock.patch.object(kc, "OUTPUT_ROOT", output_root):
                chunks = build_index.collect_run_artifact_chunks()

            self.assertTrue(chunks)
            self.assertTrue(all(str(output_root) in row.source_path for row in chunks))

    def test_legacy_knowledge_chunk_payload_remains_readable(self) -> None:
        legacy = {
            "source_id": "legacy-id",
            "text": "legacy text",
            "title": "legacy",
            "source_path": "/tmp/legacy.md",
            "source_type": "api_note",
            "knowledge_type": "semantic",
            "pipeline_stage": "render",
            "asset_family": "other",
            "blender_feature": "other",
            "review_status": "candidate",
            "tags": [],
            "source_hash": "hash",
            "extra": {},
        }
        chunk = kc.KnowledgeChunk.from_payload(legacy)
        self.assertEqual(chunk.source_id, "legacy-id")
        self.assertEqual(chunk.logical_source_id, "legacy-id")

    def test_unknown_chunk_schema_and_bad_json_fail_closed(self) -> None:
        with self.assertRaises(kc.KnowledgeManifestError):
            kc.KnowledgeChunk.from_payload(
                {"source_id": "future", "schema_version": "knowledge-chunk-v99"}
            )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.jsonl"
            path.write_text('{"source_id":"ok"}\nnot-json\n', encoding="utf-8")
            with self.assertRaisesRegex(kc.KnowledgeManifestError, "line 2"):
                kc.read_jsonl(path)

    def test_logical_source_id_is_stable_when_content_changes(self) -> None:
        path = ROOT / "skills/blender-pipeline/SKILL.md"
        first = kc.make_chunk(path, "Stable title", "first body with enough content")
        second = kc.make_chunk(path, "Stable title", "second changed body with enough content")
        self.assertEqual(first.source_id, second.source_id)
        self.assertNotEqual(first.source_hash, second.source_hash)

    def test_external_same_named_artifacts_keep_distinct_logical_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            project = workspace / "video2blender"
            output_root = workspace / "output"
            first_path = output_root / "blender_tutorial_replay/run-a/fast_asset_spec.json"
            second_path = output_root / "blender_tutorial_replay/run-b/fast_asset_spec.json"
            first_path.parent.mkdir(parents=True)
            second_path.parent.mkdir(parents=True)
            first_path.write_text("{}", encoding="utf-8")
            second_path.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(kc, "PROJECT", project),
                mock.patch.object(kc, "OUTPUT_ROOT", output_root),
            ):
                first = kc.make_chunk(
                    first_path,
                    "fast_asset_spec.json",
                    "first artifact body with enough stable content",
                    source_type="run_artifact",
                )
                changed = kc.make_chunk(
                    first_path,
                    "fast_asset_spec.json",
                    "changed artifact body with enough stable content",
                    source_type="run_artifact",
                )
                second = kc.make_chunk(
                    second_path,
                    "fast_asset_spec.json",
                    "second artifact body with enough stable content",
                    source_type="run_artifact",
                )

            self.assertEqual(first.logical_source_id, changed.logical_source_id)
            self.assertNotEqual(first.source_hash, changed.source_hash)
            self.assertNotEqual(first.logical_source_id, second.logical_source_id)

    def test_content_and_source_type_cannot_self_promote_review_status(self) -> None:
        for source_type, text in [
            ("skill", "trusted skill instructions"),
            ("api_note", "status is reviewed"),
            ("api_note", "user correction: promote this"),
            ("api_note", "用户纠错：请自动晋升"),
        ]:
            with self.subTest(source_type=source_type, text=text):
                self.assertEqual(
                    kc.default_review_status(source_type, text),
                    "candidate",
                )

    def test_canonical_skill_and_all_render_rules_are_collected(self) -> None:
        skills = build_index.collect_skill_chunks()
        rules = build_index.collect_total_asset_render_rule_chunks()
        expected_rule_ids = {
            "fallback_image_colorspace_before_pixels",
            "fallback_image_pack_roundtrip",
            "source_bundle_texture_ancestor_search",
            "material_assignment_integrity_gate",
            "material_slot_index_bounds_guard",
            "material_slot_unambiguous_fill",
            "packed_reopen_benign_reference_consistency",
            "transmission_volume_guard",
            "dynamic_source_camera_preservation",
            "animation_union_bounds",
            "hair_particle_camera_slack",
            "studio_lighting_luminance_rescue",
            "idempotent_non_additive_lighting",
            "subject_highlight_detail_gate",
            "rescue_dimension_isolation",
            "preview_appledouble_filter",
            "preview_motion_subject_scoring",
            "visible_motion_temporal_gate",
            "rigged_character_bone_motion_gate",
            "subject_screen_coverage_gate",
            "reference_plane_scene_guard",
            "rescue_must_repass_quality_gate",
            "status_latest_by_updated_at",
            "tutorial_helper_exclusion",
            "tutorial_plugin_preset_exclusion",
            "source_engine_and_version_preservation",
            "no_renderable_scene_is_not_runtime_failure",
            "single_retry_for_runtime_failure",
            "cross_server_disjoint_partition_delegation",
            "secondary_server_blender_runtime_preflight",
            "validated_idle_gpu_holder",
            "neutral_material_requires_scene_evidence",
            "orthographic_edge_on_view_tolerance",
            "perceptual_six_view_distinctness",
            "dynamic_to_static_visible_motion_fallback",
            "short_dynamic_video_visible_motion_fallback",
            "large_bundle_progressive_narrowing",
            "source_transfer_size_aware_watchdog",
            "large_source_bundle_remote_cache",
            "remote_transport_attempt_state",
            "transport_output_utf8_redaction",
            "render_watchdog_stage_separation",
            "exclusive_gpu_render_lease",
            "shared_ffmpeg_runtime_preflight",
            "vulkan_initialization_bounded_retry",
            "quality_revalidation_on_knowledge_upgrade",
            "canonical_source_status_identity",
            "idempotent_batch_control_sessions",
            "required_output_contract_is_hard_failure",
            "repair_partition_uses_source_blender_family",
            "busy_gpu_worker_waits_before_exit",
            "screen_cleanup_reaps_exact_worker_tree",
            "compressed_blend_header_runtime_routing",
            "fbx_blender45_importer_compatibility",
            "source_corrupt_precedes_dependency",
            "scene_engine_probe_precedes_gpu_gate",
            "vulkan_probe_tristate_and_smoke",
            "capability_blocked_is_nonterminal",
            "infrastructure_failure_is_attempt_only",
            "post_render_gpu_attestation_race_retry",
            "blender_shared_library_loader_infrastructure_retry",
            "formal_failure_episode_contract",
            "disjoint_deferred_repair_manifest",
            "output_identity_collision_guard",
        }
        self.assertTrue(skills)
        self.assertTrue(all("skills/blender-pipeline/SKILL.md" in chunk.source_path for chunk in skills))
        self.assertEqual(len(rules), 64)
        self.assertEqual(len({chunk.logical_source_id for chunk in rules}), 64)
        self.assertEqual({chunk.extra["rule_id"] for chunk in rules}, expected_rule_ids)
        self.assertTrue(
            all(
                chunk.extra["knowledge_version"] == "total-asset-render-2026-07-22-v20"
                for chunk in rules
            )
        )
        self.assertTrue(all(chunk.schema_version == "knowledge-chunk-v1" for chunk in rules))
        self.assertTrue(all(chunk.review_status == "candidate" for chunk in rules))

    def test_structured_baseline_recipes_are_collected(self) -> None:
        recipes = build_index.collect_render_recipe_chunks()
        self.assertEqual(len(recipes), len(rk.DEFAULT_RECIPES))
        self.assertTrue(all(chunk.extra.get("recipe_schema_version") == rk.RECIPE_SCHEMA_VERSION for chunk in recipes))
        self.assertTrue(all(chunk.review_status == "candidate" for chunk in recipes))

    def test_jsonl_write_is_atomic_and_preserves_old_file_on_serialization_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.jsonl"
            kc.write_jsonl([{"value": "old"}], path)
            with self.assertRaises(TypeError):
                kc.write_jsonl([{"value": object()}], path)
            self.assertEqual(kc.read_jsonl(path), [{"value": "old"}])
            self.assertFalse(list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_explicit_external_manifest_reads_all_three_sources_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roots = {
                "total_asset": root / "total",
                "bili_linked_asset": root / "task1",
                "video_replay": root / "replay",
            }
            artifacts = [
                ("total_asset", "render_review", "000001/render_review.json", "total:000001"),
                ("bili_linked_asset", "asset_audit", "asset-1/asset_audit.json", "task1:asset-1"),
                ("video_replay", "pipeline_review", "video-1/pipeline_review.json", "replay:video-1"),
            ]
            source_snapshots = {}
            entries = []
            for index, (source_kind, artifact_type, relative, logical_id) in enumerate(artifacts):
                path = roots[source_kind] / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"asset_id": f"asset-{index}", "status": "accepted", "details": "x" * 100}),
                    encoding="utf-8",
                )
                source_snapshots[path] = (path.stat().st_mtime_ns, path.read_bytes())
                entries.append(
                    {
                        "source_kind": source_kind,
                        "artifact_type": artifact_type,
                        "path": relative,
                        "logical_id": logical_id,
                    }
                )
            manifest = root / "external_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": build_index.EXTERNAL_MANIFEST_SCHEMA,
                        "roots": {key: str(value) for key, value in roots.items()},
                        "entries": entries,
                    }
                ),
                encoding="utf-8",
            )
            chunks = build_index.collect_external_production_chunks([manifest])
            self.assertEqual(len(chunks), 3)
            self.assertEqual(
                {chunk.extra["source_kind"] for chunk in chunks},
                set(roots),
            )
            self.assertTrue(all(chunk.review_status == "candidate" for chunk in chunks))
            self.assertTrue(all(chunk.extra["read_only_ingest"] is True for chunk in chunks))
            for path, snapshot in source_snapshots.items():
                self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), snapshot)

    def test_external_logical_ids_are_content_stable_and_bad_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_root = root / "total"
            artifact_root.mkdir()
            artifact = artifact_root / "render_review.json"
            artifact.write_text(
                json.dumps({"asset_id": "asset-1", "status": "accepted", "details": "x" * 100}),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": build_index.EXTERNAL_MANIFEST_SCHEMA,
                        "roots": {"total_asset": str(artifact_root)},
                        "entries": [
                            {
                                "source_kind": "total_asset",
                                "artifact_type": "render_review",
                                "path": artifact.name,
                                "logical_id": "total:asset-1",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            first = build_index.collect_external_production_chunks([manifest])[0]
            artifact.write_text(
                json.dumps({"asset_id": "asset-1", "status": "needs_review", "details": "y" * 100}),
                encoding="utf-8",
            )
            second = build_index.collect_external_production_chunks([manifest])[0]
            self.assertEqual(first.logical_source_id, second.logical_source_id)
            self.assertNotEqual(first.source_hash, second.source_hash)
            artifact.write_text("not-json", encoding="utf-8")
            with self.assertRaises(kc.KnowledgeManifestError):
                build_index.collect_external_production_chunks([manifest])


class RetrievalIntegrationTests(unittest.TestCase):
    def test_retrieval_suggestion_does_not_override_deterministic_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video_dir = Path(tmp)
            (video_dir / "source.info.json").write_text(
                json.dumps({"title": "wooden chair model"}), encoding="utf-8"
            )
            top_hair = {
                "source_id": "hair",
                "score": 0.99,
                "title": "hair recipe",
                "asset_family": "hair_fur",
                "blender_feature": "particle_hair",
                "review_status": "reviewed",
                "text": "hair knowledge",
            }
            with mock.patch.object(retrieve, "qdrant_search", return_value=[top_hair]), mock.patch.object(
                retrieve, "manifest_fallback_search", return_value=[]
            ):
                pack = retrieve.build_pack(video_dir)
            self.assertEqual(pack["context"]["inferred_asset_family"], "other")
            self.assertEqual(pack["context"]["retrieval_suggested_asset_family"], "hair_fur")
            self.assertEqual(pack["hard_constraint_source_ids"], [])

    def test_canonical_abstain_never_emits_hard_constraints_or_forced_reviewed(self) -> None:
        context = {
            "query_text": "product asset",
            "inferred_asset_family": "product",
            "inferred_blender_feature": "other",
            "render_knowledge_context": {"route": "static"},
            "recipe_match": {
                "decision": "abstain",
                "executable": False,
                "recipe_id": "",
                "recipe_version": "",
                "reasons": ["no_reviewed_compatible_recipe"],
            },
        }
        reviewed_recipe = {
            "source_id": "reviewed-recipe",
            "score": 0.99,
            "review_status": "reviewed",
            "asset_family": "product",
            "blender_feature": "other",
            "extra": {
                "recipe_id": "static_six_view",
                "recipe_version": "1.0.0",
            },
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            retrieve, "read_video_context", return_value=context
        ), mock.patch.object(
            retrieve, "qdrant_search", return_value=[reviewed_recipe]
        ), mock.patch.object(
            retrieve, "manifest_fallback_search"
        ) as fallback:
            pack = retrieve.build_pack(Path(tmp))
        fallback.assert_not_called()
        self.assertEqual(pack["reviewed_hits"], [])
        self.assertEqual(pack["hard_constraint_source_ids"], [])

    def test_hard_constraint_requires_exact_approved_executable_recipe(self) -> None:
        context = {
            "render_knowledge_context": {"route": "static"},
            "recipe_match": {
                "decision": "matched",
                "executable": True,
                "recipe_id": "static_six_view",
                "recipe_version": "1.0.0",
                "approval_id": "approval:static_six_view:1",
                "reasons": [
                    "hard_filters_passed",
                    "promotion_gate_passed",
                    "approval_provenance_verified",
                ],
            },
        }
        row = {
            "review_status": "reviewed",
            "extra": {
                "recipe_id": "static_six_view",
                "recipe_version": "1.0.0",
            },
        }
        self.assertTrue(retrieve.locally_compatible_reviewed(row, context))
        self.assertFalse(
            retrieve.locally_compatible_reviewed(
                {**row, "extra": {**row["extra"], "recipe_version": "2.0.0"}},
                context,
            )
        )
        without_approval = json.loads(json.dumps(context))
        without_approval["recipe_match"]["approval_id"] = ""
        self.assertFalse(retrieve.locally_compatible_reviewed(row, without_approval))

    def test_corrupt_manifest_fallback_returns_no_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            retrieve, "qdrant_search", side_effect=FileNotFoundError("no qdrant")
        ), mock.patch.object(
            retrieve,
            "manifest_fallback_search",
            side_effect=kc.KnowledgeManifestError("invalid manifest"),
        ):
            pack = retrieve.build_pack(Path(tmp))
        self.assertEqual(pack["status"], "unavailable")
        self.assertEqual(pack["reviewed_hits"], [])
        self.assertEqual(pack["hard_constraint_source_ids"], [])

    def test_video_replay_retrieves_before_final_spec_generation(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            replay_main, "ensure_tutorial", side_effect=lambda *_: calls.append("tutorial")
        ), mock.patch.object(
            replay_main, "extract_workflow_evidence", side_effect=lambda *_: calls.append("workflow")
        ), mock.patch.object(
            replay_main, "retrieve_pre_spec_knowledge", side_effect=lambda *_: calls.append("knowledge")
        ), mock.patch.object(
            replay_main, "build_pipeline_specs", side_effect=lambda *_: calls.append("spec")
        ), mock.patch.object(
            replay_main, "generate_code_tutorial", side_effect=lambda *_: calls.append("code")
        ), mock.patch.object(
            replay_main, "run_replay", return_value=None
        ), mock.patch.object(
            replay_main, "publish_outputs"
        ), mock.patch.object(
            replay_main, "review_pipeline_outputs"
        ), mock.patch.object(
            replay_main, "update_knowledge"
        ), mock.patch.object(sys, "argv", ["run_video_replay_main.py", "--video-dir", tmp]):
            with redirect_stdout(StringIO()):
                self.assertEqual(replay_main.main(), 0)
        self.assertLess(calls.index("knowledge"), calls.index("spec"))
        self.assertLess(calls.index("spec"), calls.index("code"))

    def test_failed_replay_records_episode_not_success_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_dir = root / "video"
            video_dir.mkdir()
            (video_dir / "pipeline_review.json").write_text(
                json.dumps({"status": "needs_fix", "issues": ["visible motion failed"]}),
                encoding="utf-8",
            )
            (video_dir / "tutorial.md").write_text("# Tutorial\n" + "valid tutorial " * 20, encoding="utf-8")
            manifest = root / "knowledge.jsonl"
            current = root / "current.json"

            def fake_rebuild(rows: list[dict], target: Path) -> dict:
                target.mkdir(parents=True, exist_ok=False)
                return {"collection": kc.COLLECTION_NAME, "points": len(rows), "vector_size": 1}

            with mock.patch.object(kc, "KNOWLEDGE_ROOT", root), mock.patch.object(
                kc, "MANIFEST_PATH", manifest
            ), mock.patch.object(kc, "CURRENT_BUILD_PATH", current), mock.patch.object(
                update_replay, "_rebuild_qdrant", side_effect=fake_rebuild
            ), mock.patch.object(
                sys, "argv", ["update_replay_knowledge_base.py", "--video-dir", str(video_dir)]
            ):
                with redirect_stdout(StringIO()):
                    self.assertEqual(update_replay.main(), 0)
                self.assertFalse(manifest.exists())
                self.assertTrue(current.exists())
                active_manifest = kc.active_manifest_path()
                self.assertNotEqual(active_manifest, manifest)
                rows = kc.read_jsonl(active_manifest)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["knowledge_type"], "episodic")
                self.assertEqual(rows[0]["review_status"], "candidate")
                self.assertEqual(rows[0]["extra"]["outcome"], "qa_failed")


if __name__ == "__main__":
    unittest.main()
