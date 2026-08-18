from __future__ import annotations

import copy
import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import highqal_reference_profile as reference  # noqa: E402
import highqal_source_priority as highqal  # noqa: E402
import update_high_quali_video_csv as updater  # noqa: E402


TASK_FIELDS = [
    "asset_id",
    "status",
    "model_file",
    "视频链接",
    "标题",
    "分类",
    "素材链接",
    "failure_category",
]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def profile_for(path: Path, *, motion: bool = False, luma: float = 90.0) -> dict[str, object]:
    canonical = path.resolve(strict=True)
    digest = highqal.sha256_file(canonical)
    return {
        "schema": reference.PROFILE_SCHEMA,
        "path": str(canonical),
        "sha256": digest,
        "size_bytes": canonical.stat().st_size,
        "decodable": True,
        "sampling": {"sample_fps": 2.0, "max_samples": 20, "sample_size": 64},
        "codec_name": "h264",
        "width": 1280,
        "height": 720,
        "fps": 24.0,
        "duration_seconds": 5.0,
        "declared_frame_count": 120,
        "sample_count": 10,
        "sample_rgb_sha256": "a" * 64,
        "motion_evidence": {
            "mean_frame_mad": 5.0 if motion else 0.2,
            "p95_frame_mad": 6.0 if motion else 0.4,
            "visible_motion": motion,
        },
        "luma_evidence": {
            "mean": luma,
            "stddev": 20.0,
            "p95": min(255.0, luma + 30.0),
            "overexposed_pixel_fraction": 0.01,
            "near_black_pixel_fraction": 0.01,
            "frame_mean_range": 4.0,
        },
        "color_evidence": {
            "mean_rgb": [80.0, 90.0, 100.0],
            "mean_saturation": 0.2,
            "dominant_hue_degrees": 210.0,
            "dominant_hue_fraction": 0.4,
        },
    }


class HighqalSourcePriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.task1 = self.root / "task1.csv"
        self.high_csv = self.root / "high_quali.csv"
        self.highqal_root = self.root / "highqal-data"
        self.video_root = self.root / "videos"
        self.highqal_root.mkdir()
        self.video_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_source(
        self,
        *,
        asset_id: str,
        bvid: str,
        suffix: str = ".blend",
        reference_video: bool = True,
    ) -> tuple[dict[str, str], dict[str, str], Path, Path | None]:
        source_root = self.root / "linked_assets" / asset_id
        source_root.mkdir(parents=True)
        model = source_root / f"asset{suffix}"
        model.write_bytes((asset_id + suffix).encode("utf-8") * 20)
        reference_path = None
        if reference_video:
            reference_dir = self.highqal_root / f"old_{bvid}"
            reference_dir.mkdir()
            reference_path = reference_dir / "source.mp4"
            reference_path.write_bytes((bvid + "-video").encode("utf-8") * 20)
        task_row = {
            "asset_id": asset_id,
            "status": "rendered",
            "model_file": str(model),
            "视频链接": f"https://www.bilibili.com/video/{bvid}/",
            "标题": f"title-{asset_id}",
            "分类": "animation",
            "素材链接": f"https://example.test/{asset_id}.zip",
            "failure_category": "",
        }
        high_row = {field: "" for field in updater.FIELDS}
        high_row.update({
            "平台": "Bilibili",
            "视频ID": bvid,
            "视频链接": task_row["视频链接"],
            "视频标题": task_row["标题"],
            "素材链接": task_row["素材链接"],
            "链接验证状态": "unverified_detected_link",
        })
        return task_row, high_row, model, reference_path

    def build(
        self,
        task_rows: list[dict[str, str]],
        high_rows: list[dict[str, str]],
        *,
        wave: int = 1,
        previous: tuple[Path, ...] = (),
        motion_paths: set[str] | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        write_csv(self.task1, TASK_FIELDS, task_rows)
        write_csv(self.high_csv, updater.FIELDS, high_rows)
        moving = motion_paths or set()
        return highqal.build_wave_manifest(
            task1_status=self.task1,
            high_quali_csv=self.high_csv,
            highqal_root=self.highqal_root,
            video_root=self.video_root,
            knowledge_generation="knowledge-test-v1",
            wave=wave,
            previous_manifests=previous,
            profile_builder=lambda path: profile_for(
                path, motion=str(path.resolve()) in moving
            ),
            now_epoch=100.0,
        )

    def build_explicit_canary_fixture(
        self,
        *,
        routes: tuple[str, str, str] = ("static", "dynamic", "static"),
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        Path,
        Path,
        Path,
        Path,
        list[dict[str, object]],
    ]:
        one = self.add_source(asset_id="001_BV1234567890", bvid="BV1234567890")
        two = self.add_source(asset_id="002_BVABCDEFGHIJ", bvid="BVABCDEFGHIJ")
        three = self.add_source(
            asset_id="003_BV0987654321", bvid="BV0987654321", suffix=".fbx"
        )
        manifest, _ = self.build(
            [one[0], two[0], three[0]],
            [one[1], two[1], three[1]],
            motion_paths={str(two[3].resolve())},
        )
        by_asset = {str(item["asset_id"]): item for item in manifest["items"]}
        selected_ids = [
            str(by_asset[one[0]["asset_id"]]["work_item_id"]),
            str(by_asset[two[0]["asset_id"]]["work_item_id"]),
            str(by_asset[three[0]["asset_id"]]["work_item_id"]),
        ]
        canary = highqal.select_canary_manifest(
            manifest,
            count=3,
            work_item_ids=selected_ids,
            role_evidence=(
                "Blender scene audit found no time-dependent animation",
                "Blender scene audit found non-camera Action and driver motion",
                "Frozen FBX source selected for import validation",
            ),
        )
        full_path = self.root / "full.json"
        canary_path = self.root / "canary.json"
        highqal.atomic_write_json(full_path, manifest)
        highqal.atomic_write_json(canary_path, canary)
        final_root = self.root / "final"
        statuses: list[dict[str, object]] = []
        for item, route in zip(canary["items"], routes):
            final_dir = final_root / route / str(item["output_key"])
            final_dir.mkdir(parents=True)
            asset_path = final_dir / "asset.blend"
            asset_path.write_bytes(b"published-blend")
            if route == "dynamic":
                media_path = final_dir / "final_effect.mp4"
                media_path.write_bytes(b"published-video")
            else:
                (final_dir / "six_views").mkdir()
                media_path = final_dir / "six_views/iso.png"
                media_path.write_bytes(b"published-iso")
            statuses.append({
                "status": "accepted",
                "published": True,
                "workload_generation": canary["generation"],
                "work_item_id": item["work_item_id"],
                "asset_id": item["asset_id"],
                "identity_key": item["identity_key"],
                "model_sha256": item["model_sha256"],
                "reference_sha256": item["reference_sha256"],
                "render_route": route,
                "final_dir": str(final_dir),
                "asset_blend_sha256": highqal.sha256_file(asset_path),
                "asset_blend_size_bytes": asset_path.stat().st_size,
                "media_sha256": highqal.sha256_file(media_path),
                "media_size_bytes": media_path.stat().st_size,
            })
        status_path = self.root / "status.jsonl"
        status_path.write_text(
            "".join(json.dumps(row) + "\n" for row in statuses),
            encoding="utf-8",
        )
        return (
            manifest,
            canary,
            full_path,
            canary_path,
            final_root,
            status_path,
            statuses,
        )

    def test_v2_manifest_freezes_authority_source_reference_hashes_and_composite_identity(self) -> None:
        task, high, model, video = self.add_source(
            asset_id="001_BV1234567890", bvid="BV1234567890"
        )
        manifest, quarantine = self.build([task], [high])
        self.assertEqual(manifest["schema"], highqal.MANIFEST_SCHEMA)
        self.assertEqual(manifest["source_counts"]["wave_ready"], 1)
        self.assertEqual(quarantine["items"], [])
        item = manifest["items"][0]
        self.assertEqual(item["canonical_model_path"], str(model.resolve()))
        self.assertEqual(item["model_sha256"], highqal.sha256_file(model))
        self.assertEqual(item["reference_sha256"], highqal.sha256_file(video))
        self.assertEqual(item["reference_profile"]["schema"], "reference_profile.v1")
        expected = hashlib.sha256(
            f"{task['asset_id']}\0{model.resolve()}".encode("utf-8")
        ).hexdigest()
        self.assertEqual(item["identity_key"], f"highqal_source:{expected}")
        self.assertEqual(item["work_item_id"], f"{task['asset_id']}--{expected[:16]}")
        self.assertRegex(manifest["snapshots"]["task1_status"]["sha256"], r"^[0-9a-f]{64}$")
        highqal.validate_manifest(manifest)

    def test_wave1_and_wave2_are_derived_without_a_hardcoded_count(self) -> None:
        one = self.add_source(asset_id="001_BV1234567890", bvid="BV1234567890")
        two = self.add_source(
            asset_id="002_BVABCDEFGHIJ", bvid="BVABCDEFGHIJ", reference_video=False
        )
        wave1, _ = self.build([one[0], two[0]], [one[1], two[1]])
        self.assertEqual(len(wave1["items"]), 1)
        self.assertEqual(len(wave1["pending_reference_video"]), 1)
        wave1_path = self.root / "wave1.json"
        highqal.atomic_write_json(wave1_path, wave1)

        new_reference_dir = self.highqal_root / "later_BVABCDEFGHIJ"
        new_reference_dir.mkdir()
        new_reference = new_reference_dir / "source.mp4"
        new_reference.write_bytes(b"later-video" * 30)
        wave2, _ = self.build(
            [one[0], two[0]],
            [one[1], two[1]],
            wave=2,
            previous=(wave1_path,),
        )
        self.assertEqual(len(wave2["items"]), 1)
        self.assertEqual(wave2["items"][0]["asset_id"], two[0]["asset_id"])
        self.assertEqual(wave2["previous_generations"], [wave1["generation"]])

    def test_permanent_reference_access_failure_is_terminal_not_requeued(self) -> None:
        permanent = self.add_source(
            asset_id="001_BV1234567890",
            bvid="BV1234567890",
            reference_video=False,
        )
        retryable = self.add_source(
            asset_id="002_BVABCDEFGHIJ",
            bvid="BVABCDEFGHIJ",
            reference_video=False,
        )
        ledger = self.video_root / "download_results_highqal_rest_7_21_wave0002.csv"
        result_fields = ["时间", "状态", "视频链接", "视频标题", "错误"]
        write_csv(
            ledger,
            result_fields,
            [
                {
                    "时间": "2026-07-22 10:00:00",
                    "状态": "auth_required",
                    "视频链接": permanent[0]["视频链接"],
                    "视频标题": permanent[0]["标题"],
                    "错误": "paid or creator-exclusive video",
                },
                {
                    "时间": "2026-07-22 10:00:01",
                    "状态": "failed",
                    "视频链接": retryable[0]["视频链接"],
                    "视频标题": retryable[0]["标题"],
                    "错误": "connection timed out",
                },
            ],
        )

        manifest, _ = self.build(
            [permanent[0], retryable[0]], [permanent[1], retryable[1]]
        )

        self.assertEqual(len(manifest["terminal_reference_video"]), 1)
        self.assertEqual(
            manifest["terminal_reference_video"][0]["bvid"], "BV1234567890"
        )
        self.assertEqual(
            manifest["terminal_reference_video"][0]["reference_terminal_outcome"]["status"],
            "auth_required",
        )
        self.assertEqual(len(manifest["pending_reference_video"]), 1)
        self.assertEqual(manifest["pending_reference_video"][0]["bvid"], "BVABCDEFGHIJ")
        queue = self.root / "next-wave.csv"
        self.assertEqual(
            highqal.write_reference_download_queue(
                queue, manifest["pending_reference_video"]
            ),
            1,
        )
        with queue.open(encoding="utf-8-sig", newline="") as handle:
            queued = list(csv.DictReader(handle))
        self.assertEqual([value["视频ID"] for value in queued], ["BVABCDEFGHIJ"])

    def test_duplicate_bvid_and_duplicate_asset_fail_closed(self) -> None:
        one = self.add_source(asset_id="001_BV1234567890", bvid="BV1234567890")
        write_csv(self.task1, TASK_FIELDS, [one[0]])
        write_csv(self.high_csv, updater.FIELDS, [one[1], one[1]])
        with self.assertRaisesRegex(highqal.HighqalDataError, "duplicate high_quali BVID"):
            highqal.derive_highqal_waves(
                task1_status=self.task1,
                high_quali_csv=self.high_csv,
                highqal_root=self.highqal_root,
                video_root=self.video_root,
                profile_builder=lambda path: profile_for(path),
            )

        duplicate_task = dict(one[0])
        duplicate_task["model_file"] = str(one[2])
        write_csv(self.high_csv, updater.FIELDS, [one[1]])
        write_csv(self.task1, TASK_FIELDS, [one[0], duplicate_task])
        with self.assertRaisesRegex(highqal.HighqalDataError, "duplicate Task 1"):
            highqal.derive_highqal_waves(
                task1_status=self.task1,
                high_quali_csv=self.high_csv,
                highqal_root=self.highqal_root,
                video_root=self.video_root,
                profile_builder=lambda path: profile_for(path),
            )

    def test_manifest_rejects_tampered_hash_and_generation(self) -> None:
        task, high, _model, _video = self.add_source(
            asset_id="001_BV1234567890", bvid="BV1234567890"
        )
        manifest, _ = self.build([task], [high])
        tampered = copy.deepcopy(manifest)
        tampered["items"][0]["reference_sha256"] = "0" * 64
        with self.assertRaisesRegex(highqal.HighqalDataError, "profile hash differs"):
            highqal.validate_manifest(tampered)
        tampered = copy.deepcopy(manifest)
        tampered["generation"] = "0" * 64
        with self.assertRaisesRegex(highqal.HighqalDataError, "generation"):
            highqal.validate_manifest(tampered)

    def test_pre_render_file_attestation_rehashes_and_rejects_source_drift(self) -> None:
        task, high, model, _video = self.add_source(
            asset_id="001_BV1234567890", bvid="BV1234567890"
        )
        manifest, _ = self.build([task], [high])
        item = manifest["items"][0]
        attestation = highqal.verify_manifest_item_files(item)
        self.assertEqual(attestation["model_sha256"], item["model_sha256"])
        model.write_bytes(model.read_bytes() + b"drift")
        with self.assertRaisesRegex(
            highqal.HighqalDataError, "source size differs|source hash differs"
        ):
            highqal.verify_manifest_item_files(item)

    def test_freeze_has_no_fixed_179_expectation_and_is_idempotent(self) -> None:
        task, high, _model, _video = self.add_source(
            asset_id="001_BV1234567890", bvid="BV1234567890"
        )
        write_csv(self.task1, TASK_FIELDS, [task])
        write_csv(self.high_csv, updater.FIELDS, [high])
        manifest_path = self.root / "wave1.json"
        queue = self.root / "wave2.csv"
        quarantine = self.root / "quarantine.json"
        first = highqal.freeze_wave_manifest(
            path=manifest_path,
            expected_count=None,
            quarantine_path=quarantine,
            reference_download_queue=queue,
            task1_status=self.task1,
            high_quali_csv=self.high_csv,
            highqal_root=self.highqal_root,
            video_root=self.video_root,
            knowledge_generation="knowledge-test-v1",
            profile_builder=lambda path: profile_for(path),
            now_epoch=100.0,
        )
        second = highqal.freeze_wave_manifest(
            path=manifest_path,
            expected_count=None,
            quarantine_path=quarantine,
            reference_download_queue=queue,
            task1_status=self.root / "does-not-matter.csv",
            high_quali_csv=self.root / "does-not-matter-either.csv",
            highqal_root=self.highqal_root,
            video_root=self.video_root,
            knowledge_generation="different",
        )
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(len(first["items"]), 1)

    def test_canary_is_repeatable_and_covers_static_dynamic_nonblend(self) -> None:
        one = self.add_source(asset_id="001_BV1234567890", bvid="BV1234567890")
        two = self.add_source(asset_id="002_BVABCDEFGHIJ", bvid="BVABCDEFGHIJ")
        three = self.add_source(
            asset_id="003_BV0987654321", bvid="BV0987654321", suffix=".fbx"
        )
        moving = {str(two[3].resolve())}
        manifest, _ = self.build(
            [one[0], two[0], three[0]],
            [one[1], two[1], three[1]],
            motion_paths=moving,
        )
        canary = highqal.select_canary_manifest(manifest, count=3)
        repeated = highqal.select_canary_manifest(manifest, count=3)
        self.assertEqual(canary["generation"], repeated["generation"])
        formats = [item["model_format"] for item in canary["items"]]
        motions = [item["reference_profile"]["motion_evidence"]["visible_motion"] for item in canary["items"]]
        self.assertIn(".fbx", formats)
        self.assertIn(True, motions)
        self.assertIn(False, motions)

    def test_explicit_scene_audited_canary_preserves_exact_roles_and_evidence(self) -> None:
        static = self.add_source(asset_id="001_BV1234567890", bvid="BV1234567890")
        dynamic = self.add_source(asset_id="002_BVABCDEFGHIJ", bvid="BVABCDEFGHIJ")
        imported = self.add_source(
            asset_id="003_BV0987654321", bvid="BV0987654321", suffix=".fbx"
        )
        manifest, _ = self.build(
            [static[0], dynamic[0], imported[0]],
            [static[1], dynamic[1], imported[1]],
        )
        selected_ids = [
            manifest["items"][0]["work_item_id"],
            manifest["items"][1]["work_item_id"],
            manifest["items"][2]["work_item_id"],
        ]
        evidence = ["static scene audit", "dynamic scene audit", "FBX import audit"]
        canary = highqal.select_canary_manifest(
            manifest,
            count=3,
            work_item_ids=selected_ids,
            role_evidence=evidence,
        )
        subset = canary["subset"]
        self.assertEqual(subset["selection_policy"], "explicit_scene_audited_v1")
        self.assertEqual(subset["selected_work_item_ids"], selected_ids)
        self.assertEqual(
            subset["roles"],
            ["static_scene_audited", "dynamic_scene_audited", "nonblend_import"],
        )
        self.assertEqual(
            subset["role_evidence"],
            [
                {"role": role, "work_item_id": work_id, "evidence": proof}
                for role, work_id, proof in zip(subset["roles"], selected_ids, evidence)
            ],
        )
        self.assertEqual(
            [item["work_item_id"] for item in canary["items"]], selected_ids
        )

    def test_explicit_scene_audited_canary_rejects_invalid_selection(self) -> None:
        first = self.add_source(asset_id="001_BV1234567890", bvid="BV1234567890")
        second = self.add_source(asset_id="002_BVABCDEFGHIJ", bvid="BVABCDEFGHIJ")
        third = self.add_source(
            asset_id="003_BV0987654321", bvid="BV0987654321", suffix=".fbx"
        )
        manifest, _ = self.build(
            [first[0], second[0], third[0]],
            [first[1], second[1], third[1]],
        )
        ids = [item["work_item_id"] for item in manifest["items"]]
        evidence = ["static", "dynamic", "nonblend"]
        invalid_cases = (
            {"count": 1, "work_item_ids": ids, "role_evidence": evidence},
            {"count": 3, "work_item_ids": [ids[0], ids[0], ids[2]], "role_evidence": evidence},
            {"count": 3, "work_item_ids": [ids[0], "missing", ids[2]], "role_evidence": evidence},
            {"count": 3, "work_item_ids": ids, "role_evidence": ["static", "", "nonblend"]},
            {"count": 3, "work_item_ids": [ids[2], ids[1], ids[0]], "role_evidence": evidence},
        )
        for kwargs in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(highqal.HighqalDataError):
                    highqal.select_canary_manifest(manifest, **kwargs)

    def test_retry_canary_pair_requires_current_knowledge_and_exact_parent(self) -> None:
        (
            manifest,
            canary,
            full_path,
            canary_path,
            _final_root,
            _status,
            _statuses,
        ) = self.build_explicit_canary_fixture()

        full, selected = highqal.validate_retry_canary_manifests(
            full_manifest_path=full_path,
            canary_manifest_path=canary_path,
            knowledge_generation=str(manifest["knowledge_generation"]),
        )
        self.assertEqual(full["generation"], manifest["generation"])
        self.assertEqual(selected["generation"], canary["generation"])
        with self.assertRaisesRegex(
            highqal.HighqalDataError, "knowledge generation differs"
        ):
            highqal.validate_retry_canary_manifests(
                full_manifest_path=full_path,
                canary_manifest_path=canary_path,
                knowledge_generation="newer-knowledge",
            )

    def test_retry_canary_registration_requests_atomic_terminal_replacement(self) -> None:
        (
            manifest,
            canary,
            full_path,
            canary_path,
            final_root,
            _status,
            _statuses,
        ) = self.build_explicit_canary_fixture()
        old = copy.deepcopy(canary)
        old["knowledge_generation"] = "knowledge-test-v0"
        old["generation"] = highqal._manifest_generation(
            items=old["items"],
            snapshots=old["snapshots"],
            wave=int(old["wave"]),
            knowledge_generation=str(old["knowledge_generation"]),
            subset=old["subset"],
        )
        old_path = self.root / "old-canary.json"
        highqal.atomic_write_json(old_path, old)
        expected_payload = {
            "registered": True,
            "generation": canary["generation"],
            "state": "paused",
            "counts": {"ready": 3},
            "item_count": 3,
        }
        current_payload = {
            "registered": True,
            "generation": old["generation"],
            "manifest_path": str(old_path),
            "state": "active",
            "counts": {"needs_review": 3},
            "item_count": 3,
            "active_leases": [],
        }
        with mock.patch(
            "total_asset_cycle72.priority_workload_status",
            return_value=current_payload,
        ), mock.patch(
            "total_asset_cycle72.register_priority_workload",
            return_value=expected_payload,
        ) as register:
            payload = highqal.register_retry_canary(
                db_path=self.root / "leases.sqlite3",
                full_manifest_path=full_path,
                canary_manifest_path=canary_path,
                status_path=self.root / "retry-status.jsonl",
                final_root=final_root,
                evidence_root=self.root / "retry-evidence",
                inventory=self.root / "inventory",
                expected_current_generation=str(old["generation"]),
                knowledge_generation=str(manifest["knowledge_generation"]),
            )

        self.assertEqual(payload["retry_from_generation"], old["generation"])
        kwargs = register.call_args.kwargs
        self.assertEqual(kwargs["state"], "paused")
        self.assertEqual(
            kwargs["expected_current_generation"], old["generation"]
        )
        self.assertTrue(kwargs["require_current_terminal_rejected"])

    def test_canary_adoption_receipt_requires_exact_generation_hashes_and_outputs(self) -> None:
        (
            manifest,
            canary,
            full_path,
            canary_path,
            final_root,
            status,
            statuses,
        ) = self.build_explicit_canary_fixture()
        receipt = highqal.build_canary_adoption_receipt(
            full_manifest_path=full_path,
            canary_manifest_path=canary_path,
            status_path=status,
            final_root=final_root,
        )
        self.assertEqual(receipt["full_generation"], manifest["generation"])
        self.assertEqual(len(receipt["items"]), 3)
        self.assertEqual(
            {item["render_route"] for item in receipt["items"]},
            {"static", "dynamic"},
        )
        self.assertRegex(receipt["items"][0]["asset_blend_sha256"], r"^[0-9a-f]{64}$")

        bad_statuses = copy.deepcopy(statuses)
        bad_statuses[0]["model_sha256"] = "0" * 64
        status.write_text(
            "".join(json.dumps(row) + "\n" for row in bad_statuses),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(highqal.HighqalDataError, "model_sha256 differs"):
            highqal.build_canary_adoption_receipt(
                full_manifest_path=full_path,
                canary_manifest_path=canary_path,
                status_path=status,
                final_root=final_root,
            )

    def test_canary_adoption_receipt_rejects_wrong_scene_audited_route(self) -> None:
        (
            _manifest,
            _canary,
            full_path,
            canary_path,
            final_root,
            status,
            statuses,
        ) = self.build_explicit_canary_fixture()
        cases = (
            (0, "dynamic", "static_scene_audited.*render_route static"),
            (1, "static", "dynamic_scene_audited.*render_route dynamic"),
        )
        for index, route, error in cases:
            with self.subTest(index=index, route=route):
                wrong_statuses = copy.deepcopy(statuses)
                wrong_statuses[index]["render_route"] = route
                status.write_text(
                    "".join(json.dumps(row) + "\n" for row in wrong_statuses),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(highqal.HighqalDataError, error):
                    highqal.build_canary_adoption_receipt(
                        full_manifest_path=full_path,
                        canary_manifest_path=canary_path,
                        status_path=status,
                        final_root=final_root,
                    )

    def test_canary_adoption_receipt_rejects_tampered_role_mapping_and_evidence(self) -> None:
        (
            _manifest,
            canary,
            full_path,
            canary_path,
            final_root,
            status,
            _statuses,
        ) = self.build_explicit_canary_fixture()

        def with_valid_generation(payload: dict[str, object]) -> dict[str, object]:
            payload["generation"] = highqal._manifest_generation(
                items=payload["items"],
                snapshots=payload["snapshots"],
                wave=int(payload["wave"]),
                knowledge_generation=str(payload["knowledge_generation"]),
                subset=payload["subset"],
            )
            return payload

        tampered_cases: list[tuple[str, dict[str, object], str]] = []
        wrong_selection = copy.deepcopy(canary)
        wrong_selection["subset"]["selected_work_item_ids"][:2] = reversed(
            wrong_selection["subset"]["selected_work_item_ids"][:2]
        )
        tampered_cases.append(("selected order", wrong_selection, "selected_work_item_ids"))

        wrong_roles = copy.deepcopy(canary)
        wrong_roles["subset"]["roles"][:2] = reversed(
            wrong_roles["subset"]["roles"][:2]
        )
        tampered_cases.append(("role order", wrong_roles, "roles"))

        wrong_evidence = copy.deepcopy(canary)
        wrong_evidence["subset"]["role_evidence"][0]["work_item_id"] = (
            wrong_evidence["subset"]["role_evidence"][1]["work_item_id"]
        )
        tampered_cases.append(("evidence mapping", wrong_evidence, "role evidence"))

        for label, tampered, error in tampered_cases:
            with self.subTest(label=label):
                highqal.atomic_write_json(
                    canary_path,
                    with_valid_generation(tampered),
                )
                with self.assertRaisesRegex(highqal.HighqalDataError, error):
                    highqal.build_canary_adoption_receipt(
                        full_manifest_path=full_path,
                        canary_manifest_path=canary_path,
                        status_path=status,
                        final_root=final_root,
                    )

    def test_canary_adoption_receipt_rejects_wrong_sibling_final_dir(self) -> None:
        (
            _manifest,
            _canary,
            full_path,
            canary_path,
            final_root,
            status,
            statuses,
        ) = self.build_explicit_canary_fixture()
        sibling = final_root / "static" / "wrong-sibling"
        (sibling / "six_views").mkdir(parents=True)
        (sibling / "asset.blend").write_bytes(b"published-blend")
        (sibling / "six_views/iso.png").write_bytes(b"published-iso")
        wrong_statuses = copy.deepcopy(statuses)
        wrong_statuses[0]["final_dir"] = str(sibling)
        status.write_text(
            "".join(json.dumps(row) + "\n" for row in wrong_statuses),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            highqal.HighqalDataError,
            "final_dir differs from the exact output contract",
        ):
            highqal.build_canary_adoption_receipt(
                full_manifest_path=full_path,
                canary_manifest_path=canary_path,
                status_path=status,
                final_root=final_root,
            )

    def test_canary_adoption_receipt_rejects_wrong_status_identity(self) -> None:
        (
            _manifest,
            _canary,
            full_path,
            canary_path,
            final_root,
            status,
            statuses,
        ) = self.build_explicit_canary_fixture()
        for key, value in (
            ("asset_id", "tampered-asset"),
            ("identity_key", "highqal_source:tampered"),
        ):
            with self.subTest(key=key):
                wrong_statuses = copy.deepcopy(statuses)
                wrong_statuses[0][key] = value
                status.write_text(
                    "".join(json.dumps(row) + "\n" for row in wrong_statuses),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    highqal.HighqalDataError,
                    f"accepted canary {key} differs from full manifest",
                ):
                    highqal.build_canary_adoption_receipt(
                        full_manifest_path=full_path,
                        canary_manifest_path=canary_path,
                        status_path=status,
                        final_root=final_root,
                    )

    def test_canary_adoption_receipt_rejects_wrong_landed_hash_or_size(self) -> None:
        (
            _manifest,
            _canary,
            full_path,
            canary_path,
            final_root,
            status,
            statuses,
        ) = self.build_explicit_canary_fixture()
        cases = (
            ("asset_blend_sha256", "0" * 64),
            ("media_sha256", "1" * 64),
            (
                "asset_blend_size_bytes",
                int(statuses[0]["asset_blend_size_bytes"]) + 1,
            ),
            ("media_size_bytes", int(statuses[0]["media_size_bytes"]) + 1),
        )
        for key, value in cases:
            with self.subTest(key=key):
                wrong_statuses = copy.deepcopy(statuses)
                wrong_statuses[0][key] = value
                status.write_text(
                    "".join(json.dumps(row) + "\n" for row in wrong_statuses),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    highqal.HighqalDataError,
                    f"accepted canary {key} differs from landed output",
                ):
                    highqal.build_canary_adoption_receipt(
                        full_manifest_path=full_path,
                        canary_manifest_path=canary_path,
                        status_path=status,
                        final_root=final_root,
                    )

    def test_discover_requires_local_verified_video_and_safe_source_archive(self) -> None:
        write_csv(self.high_csv, updater.FIELDS, [])
        video = self.root / "reference.mp4"
        video.write_bytes(b"video" * 50)
        archive = self.root / "source.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("project/scene.blend", b"BLENDER" * 100)
        candidate = self.root / "candidate.json"
        candidate.write_text(json.dumps({
            "schema": highqal.DISCOVERY_SCHEMA,
            "视频ID": "BV158411d77o",
            "视频链接": "https://www.bilibili.com/video/BV158411d77o/",
            "视频标题": "CyberSeer",
            "素材链接": "https://drive.google.com/file/d/example/view",
            "reference_video": str(video),
            "source_archive": str(archive),
        }), encoding="utf-8")
        result = highqal.discover_verified_pairs(
            high_quali_csv=self.high_csv,
            candidate_paths=[candidate],
            profile_builder=lambda path: profile_for(path),
        )
        self.assertEqual(result["new_bvids"], ["BV158411d77o"])
        rows = highqal.read_csv(self.high_csv)
        self.assertEqual(rows[0]["链接验证状态"], "verified_pair")
        self.assertEqual(rows[0]["入库标准"], "verified_video_and_source_pair")

        bad = self.root / "unsafe.zip"
        with zipfile.ZipFile(bad, "w") as handle:
            handle.writestr("../escape.blend", b"bad")
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        payload["source_archive"] = str(bad)
        with self.assertRaisesRegex(highqal.HighqalDataError, "unsafe path"):
            highqal.verify_discovery_candidate(
                payload, profile_builder=lambda path: profile_for(path)
            )

    def test_discover_is_the_only_csv_mutation_path_and_requires_explicit_evidence(self) -> None:
        task, high, _model, _video = self.add_source(
            asset_id="001_BV1234567890", bvid="BV1234567890"
        )
        write_csv(self.task1, TASK_FIELDS, [task])
        write_csv(self.high_csv, updater.FIELDS, [high])
        before = self.high_csv.read_bytes()
        highqal.command_plan(
            task1_status=self.task1,
            high_quali_csv=self.high_csv,
            highqal_root=self.highqal_root,
            video_root=self.video_root,
            knowledge_generation="knowledge-test-v1",
            profile_builder=lambda path: profile_for(path),
        )
        self.assertEqual(self.high_csv.read_bytes(), before)
        self.assertEqual(
            highqal.main(["discover", "--high-quali", str(self.high_csv)]), 75
        )
        self.assertEqual(self.high_csv.read_bytes(), before)

    def test_status_aggregates_auxiliary_states_and_marks_bad_json_unavailable(self) -> None:
        task, high, _model, _video = self.add_source(
            asset_id="001_BV1234567890", bvid="BV1234567890"
        )
        manifest, _ = self.build([task], [high])
        manifest_path = self.root / "manifest.json"
        highqal.atomic_write_json(manifest_path, manifest)
        download_state = self.root / "download.json"
        promotion_state = self.root / "promotion.json"
        download_state.write_text(
            json.dumps({"schema": "download.v1", "status": "running"}),
            encoding="utf-8",
        )
        promotion_state.write_text("{broken", encoding="utf-8")

        payload = highqal.command_status(
            db_path=self.root / "missing.sqlite3",
            manifest_path=manifest_path,
            reference_download_state=download_state,
            canary_promotion_state=promotion_state,
        )

        self.assertEqual(payload["manifest"]["generation"], manifest["generation"])
        self.assertFalse(payload["workload"]["registered"])
        self.assertTrue(payload["reference_download"]["available"])
        self.assertEqual(
            payload["reference_download"]["state"]["status"], "running"
        )
        self.assertFalse(payload["canary_promotion"]["available"])
        self.assertEqual(payload["canary_promotion"]["reason"], "invalid_json")
        self.assertRegex(payload["reference_download"]["sha256"], r"^[0-9a-f]{64}$")

        invalid_database = self.root / "invalid.sqlite3"
        invalid_database.write_bytes(b"not a sqlite database")
        unavailable = highqal.command_status(
            db_path=invalid_database,
            manifest_path=manifest_path,
            reference_download_state=download_state,
        )
        self.assertFalse(unavailable["workload"]["available"])
        self.assertEqual(
            unavailable["workload"]["reason"], "cycle72_database_unavailable"
        )

    def test_checkpoint_atomically_overwrites_one_stable_artifact_without_source_mutation(self) -> None:
        task, high, _model, _video = self.add_source(
            asset_id="001_BV1234567890", bvid="BV1234567890"
        )
        manifest, _ = self.build([task], [high])
        manifest_path = self.root / "manifest.json"
        highqal.atomic_write_json(manifest_path, manifest)
        download_state = self.root / "download.json"
        download_state.write_text(
            json.dumps({"schema": "download.v1", "status": "waiting"}),
            encoding="utf-8",
        )
        checkpoint_path = self.root / "logs" / "highqal_priority.current.checkpoint.json"
        sources_before = {
            manifest_path: manifest_path.read_bytes(),
            download_state: download_state.read_bytes(),
        }

        first = highqal.write_checkpoint(
            checkpoint_path,
            db_path=self.root / "missing.sqlite3",
            manifest_path=manifest_path,
            reference_download_state=download_state,
            canary_promotion_state=self.root / "missing-promotion.json",
        )
        second = highqal.write_checkpoint(
            checkpoint_path,
            db_path=self.root / "missing.sqlite3",
            manifest_path=manifest_path,
            reference_download_state=download_state,
            canary_promotion_state=self.root / "missing-promotion.json",
        )

        self.assertEqual(
            json.loads(checkpoint_path.read_text(encoding="utf-8")), second
        )
        self.assertEqual(
            list(checkpoint_path.parent.glob("highqal_priority*.checkpoint.json")),
            [checkpoint_path],
        )
        self.assertEqual(first["manifest"]["generation"], manifest["generation"])
        self.assertIn("workload", second)
        self.assertIn("reference_download", second)
        self.assertIsNone(second["canary_promotion"])
        self.assertRegex(
            second["source_files"]["manifest"]["sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertRegex(second["checkpoint_observation_sha256"], r"^[0-9a-f]{64}$")
        for path, expected in sources_before.items():
            self.assertEqual(path.read_bytes(), expected)
        self.assertFalse((self.root / "missing.sqlite3").exists())

    def test_status_cli_prints_exactly_one_json_document(self) -> None:
        task, high, _model, _video = self.add_source(
            asset_id="001_BV1234567890", bvid="BV1234567890"
        )
        manifest, _ = self.build([task], [high])
        manifest_path = self.root / "manifest.json"
        highqal.atomic_write_json(manifest_path, manifest)
        download_state = self.root / "download.json"
        promotion_state = self.root / "promotion.json"
        download_state.write_text('{"status":"complete"}', encoding="utf-8")
        promotion_state.write_text('{"status":"promoted"}', encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "highqal_source_priority.py"),
                "status",
                "--db",
                str(self.root / "missing.sqlite3"),
                "--manifest",
                str(manifest_path),
                "--reference-download-state",
                str(download_state),
                "--canary-promotion-state",
                str(promotion_state),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decoded = json.loads(result.stdout)
        self.assertEqual(decoded["command"], "status")
        self.assertEqual(decoded["reference_download"]["state"]["status"], "complete")
        self.assertEqual(decoded["canary_promotion"]["state"]["status"], "promoted")


class HighqalReferenceProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cpu_profile_collects_decodability_motion_luma_and_colour(self) -> None:
        media = self.root / "sample.mp4"
        media.write_bytes(b"not-real-but-runner-is-bounded")
        dark = bytes([20, 30, 40]) * (4 * 4)
        bright = bytes([80, 100, 140]) * (4 * 4)

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
            if "-show_entries" in command:
                payload = {
                    "streams": [{
                        "codec_name": "h264",
                        "codec_type": "video",
                        "width": 1280,
                        "height": 720,
                        "avg_frame_rate": "24/1",
                        "r_frame_rate": "24/1",
                        "duration": "5.0",
                        "nb_frames": "120",
                    }],
                    "format": {"duration": "5.0"},
                }
                return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode(), b"")
            return subprocess.CompletedProcess(command, 0, dark + bright, b"")

        profile = reference.build_reference_profile(
            media,
            sample_size=4,
            max_samples=2,
            runner=runner,
        )
        self.assertTrue(profile["decodable"])
        self.assertEqual(profile["sample_count"], 2)
        self.assertTrue(profile["motion_evidence"]["visible_motion"])
        self.assertGreater(profile["luma_evidence"]["mean"], 0)
        self.assertEqual(len(profile["color_evidence"]["mean_rgb"]), 3)

    def test_reference_gate_only_downgrades_and_catches_overexposure(self) -> None:
        ref_file = self.root / "reference.mp4"
        ref_file.write_bytes(b"reference")
        out = self.root / "output"
        (out / "six_views").mkdir(parents=True)
        iso = out / "six_views/iso.png"
        iso.write_bytes(b"iso")
        ref_profile = profile_for(ref_file, luma=80.0)
        item = {
            "reference_profile": ref_profile,
            "reference_sha256": ref_profile["sha256"],
        }
        result_profile = profile_for(iso, luma=250.0)
        result_profile["luma_evidence"]["overexposed_pixel_fraction"] = 0.9
        outcome = {
            "status": "accepted",
            "render_route": "static",
            "output_dir": str(out),
        }
        gated = reference.evaluate_reference_quality_gate(
            item, outcome, profiler=lambda _path: result_profile
        )
        self.assertEqual(gated["status"], "needs_review")
        self.assertIn("result_overexposed_relative_to_reference", gated["reference_gate"]["reasons"])

        existing = reference.evaluate_reference_quality_gate(
            item, {**outcome, "status": "failed"}, profiler=lambda _path: result_profile
        )
        self.assertEqual(existing["status"], "failed")
        self.assertEqual(existing["reference_gate"]["status"], "skipped_existing_nonaccepted")

    def test_dynamic_reference_gate_requires_five_seconds_and_visible_motion(self) -> None:
        ref_file = self.root / "reference.mp4"
        result_file = self.root / "result.mp4"
        ref_file.write_bytes(b"reference")
        result_file.write_bytes(b"result")
        ref_profile = profile_for(ref_file, motion=True)
        result_profile = profile_for(result_file, motion=False)
        result_profile["duration_seconds"] = 3.0
        gate = reference.compare_reference_profiles(
            ref_profile, result_profile, render_route="dynamic"
        )
        self.assertEqual(gate["status"], "needs_review")
        self.assertIn("dynamic_result_duration_outside_contract", gate["reasons"])
        self.assertIn("dynamic_result_has_no_visible_motion", gate["reasons"])


if __name__ == "__main__":
    unittest.main()
