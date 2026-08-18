from __future__ import annotations

import csv
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import highqal_candidate_supply as supply  # noqa: E402
import highqal_reference_profile as reference  # noqa: E402
import update_high_quali_video_csv as updater  # noqa: E402


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str] | None = None) -> None:
    chosen = fields or updater.FIELDS
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=chosen)
        writer.writeheader()
        writer.writerows(rows)


def csv_row(bvid: str, *, source_url: str = "https://example.test/source.zip") -> dict[str, str]:
    row = {field: "" for field in updater.FIELDS}
    row.update({
        "平台": "Bilibili",
        "视频ID": bvid,
        "视频链接": f"https://www.bilibili.com/video/{bvid}/",
        "视频标题": f"title-{bvid}",
        "素材链接": source_url,
        "链接验证状态": "unverified_detected_link",
    })
    return row


def fake_video_probe(path: Path) -> dict[str, object]:
    canonical = path.resolve(strict=True)
    return {
        "path": str(canonical),
        "sha256": supply.highqal.sha256_file(canonical),
        "size_bytes": canonical.stat().st_size,
        "codec_name": "h264",
        "width": 1280,
        "height": 720,
        "duration_seconds": 5.0,
    }


def fake_profile(path: Path) -> dict[str, object]:
    canonical = path.resolve(strict=True)
    return {
        "schema": reference.PROFILE_SCHEMA,
        "path": str(canonical),
        "sha256": supply.highqal.sha256_file(canonical),
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
        "motion_evidence": {"mean_frame_mad": 1.0, "p95_frame_mad": 2.0, "visible_motion": False},
        "luma_evidence": {
            "mean": 90.0,
            "stddev": 20.0,
            "p95": 120.0,
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


class HighqalCandidateSupplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.high_csv = self.root / "high_quali.csv"
        self.state_root = self.root / "state"
        self.video = self.root / "reference.mp4"
        self.video.write_bytes(b"video-evidence" * 100)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def archive(self, name: str, members: dict[str, bytes]) -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as handle:
            for member, content in members.items():
                handle.writestr(member, content)
        return path

    def candidate(self, bvid: str, source: Path) -> dict[str, str]:
        return {
            "视频ID": bvid,
            "视频链接": f"https://www.bilibili.com/video/{bvid}/",
            "素材链接": "https://example.test/source.zip",
            "reference_video": str(self.video),
            "source_archive": str(source),
        }

    def classify(self, row: dict[str, str], candidate: dict[str, object]) -> str:
        result, _ = supply.classify_candidate(
            row=row,
            candidate=candidate,
            bvid=row["视频ID"],
            network=False,
            download_root=self.root / "downloads",
            max_download_bytes=1024 * 1024,
            max_archive_bytes=1024 * 1024,
            max_archive_members=100,
            timeout=1,
            video_probe=fake_video_probe,
        )
        return str(result["classification"])

    def test_plan_preserves_original_columns_and_deduplicates_bvid(self) -> None:
        rows = [csv_row("BV1234567890"), csv_row("BV1234567890"), csv_row("BVABCDEFGHIJ")]
        write_csv(self.high_csv, rows)
        before = self.high_csv.read_bytes()
        payload = supply.plan_payload(
            high_quali_csv=self.high_csv,
            candidate_paths=[],
            state_root=self.state_root,
            start_row=1,
            limit=50,
        )
        self.assertEqual(payload["unique_bvids"], 2)
        self.assertEqual(payload["duplicate_csv_bvids"], 1)
        self.assertEqual(payload["original_fields"], updater.FIELDS)
        self.assertEqual(self.high_csv.read_bytes(), before)

    def test_explicit_candidate_list_is_exact_and_deduplicated(self) -> None:
        write_csv(self.high_csv, [csv_row("BV1234567890"), csv_row("BVABCDEFGHIJ")])
        candidate_path = self.root / "candidates.json"
        candidate_path.write_text(json.dumps([
            {"视频ID": "BVABCDEFGHIJ"},
            {"视频ID": "BVABCDEFGHIJ", "source_project": "/ignored"},
        ]), encoding="utf-8")
        payload = supply.plan_payload(
            high_quali_csv=self.high_csv,
            candidate_paths=[candidate_path],
            state_root=self.state_root,
            start_row=1,
            limit=50,
        )
        self.assertEqual(payload["selected_bvids"], ["BVABCDEFGHIJ"])
        self.assertEqual(payload["duplicate_candidate_bvids"], 1)

    def test_all_six_classifications_are_exclusive(self) -> None:
        bvid = "BV1234567890"
        row = csv_row(bvid)
        good = self.archive("good.zip", {"scene.blend": b"BLENDER"})
        unsupported = self.archive("unsupported.zip", {"scene.max": b"MAX"})
        non_asset = self.archive("notes.zip", {"readme.txt": b"hello"})

        cases: list[tuple[str, dict[str, object]]] = [
            ("verified_pair", self.candidate(bvid, good)),
            ("source_unavailable", {
                "视频ID": bvid,
                "视频链接": row["视频链接"],
                "素材链接": "",
                "reference_video": str(self.video),
            }),
            ("auth_required", {
                "视频ID": bvid,
                "视频链接": row["视频链接"],
                "素材链接": row["素材链接"],
                "提取码": "abcd",
                "reference_video": str(self.video),
            }),
            ("non_asset_resource", self.candidate(bvid, non_asset)),
            ("unsupported_format", self.candidate(bvid, unsupported)),
            ("video_unavailable", {
                **self.candidate(bvid, good),
                "reference_video": str(self.root / "missing.mp4"),
            }),
        ]
        observed = [self.classify(row if expected != "source_unavailable" else {**row, "素材链接": ""}, value) for expected, value in cases]
        self.assertEqual(observed, [expected for expected, _ in cases])
        self.assertEqual(set(observed), set(supply.CLASSIFICATIONS))

    def test_bad_zip_and_path_traversal_are_unsupported(self) -> None:
        bvid = "BV1234567890"
        bad = self.root / "bad.zip"
        bad.write_bytes(b"not-a-zip")
        traversal = self.archive("traversal.zip", {"../escape.blend": b"bad"})
        self.assertEqual(self.classify(csv_row(bvid), self.candidate(bvid, bad)), "unsupported_format")
        self.assertEqual(self.classify(csv_row(bvid), self.candidate(bvid, traversal)), "unsupported_format")

        symlink = self.root / "symlink.zip"
        with zipfile.ZipFile(symlink, "w") as handle:
            info = zipfile.ZipInfo("project/link.blend")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            handle.writestr(info, "../outside.blend")
        self.assertEqual(
            self.classify(csv_row(bvid), self.candidate(bvid, symlink)),
            "unsupported_format",
        )

    def test_existing_materialized_tree_is_reverified_not_trusted(self) -> None:
        archive = self.archive(
            "materialize.zip",
            {"project/scene.blend": b"BLENDER", "textures/albedo.png": b"PNG"},
        )
        destination = self.root / "materialized"
        supply._extract_archive_atomic(
            archive,
            destination,
            max_archive_bytes=1024,
            max_archive_members=100,
        )
        (destination / "textures/albedo.png").write_bytes(b"TAMPERED")
        with self.assertRaisesRegex(supply.SupplyError, "tree differs"):
            supply._extract_archive_atomic(
                archive,
                destination,
                max_archive_bytes=1024,
                max_archive_members=100,
            )

    def test_http_auth_and_unavailable_classifications(self) -> None:
        bvid = "BV1234567890"
        row = csv_row(bvid)

        def auth_downloader(*_args: object, **_kwargs: object) -> tuple[Path, dict[str, object]]:
            raise supply.HttpEvidenceError("auth_required", "HTTP 403", status=403)

        result, _ = supply.classify_candidate(
            row=row,
            candidate={"reference_video": str(self.video)},
            bvid=bvid,
            network=True,
            download_root=self.root / "downloads",
            max_download_bytes=100,
            max_archive_bytes=100,
            max_archive_members=10,
            timeout=1,
            video_probe=fake_video_probe,
            source_downloader=auth_downloader,
            capacity_guard=lambda *_args, **_kwargs: {"tier": "ok"},
        )
        self.assertEqual(result["classification"], "auth_required")

        def missing_downloader(*_args: object, **_kwargs: object) -> tuple[Path, dict[str, object]]:
            raise supply.HttpEvidenceError("unavailable", "HTTP 404", status=404)

        result, _ = supply.classify_candidate(
            row=row,
            candidate={"reference_video": str(self.video)},
            bvid=bvid,
            network=True,
            download_root=self.root / "downloads2",
            max_download_bytes=100,
            max_archive_bytes=100,
            max_archive_members=10,
            timeout=1,
            video_probe=fake_video_probe,
            source_downloader=missing_downloader,
            capacity_guard=lambda *_args, **_kwargs: {"tier": "ok"},
        )
        self.assertEqual(result["classification"], "source_unavailable")

    def test_run_promotes_only_verified_pair_through_authoritative_writer(self) -> None:
        bvid = "BV1234567890"
        write_csv(self.high_csv, [csv_row(bvid)])
        archive = self.archive("source.zip", {"project/scene.blend": b"BLENDER"})
        candidate_path = self.root / "candidate.json"
        candidate_path.write_text(json.dumps(self.candidate(bvid, archive)), encoding="utf-8")

        payload = supply.run_supply(
            high_quali_csv=self.high_csv,
            candidate_paths=[candidate_path],
            state_root=self.state_root,
            start_row=1,
            limit=1,
            max_download_bytes=1024,
            max_archive_bytes=1024,
            max_archive_members=100,
            timeout=1,
            video_probe=fake_video_probe,
            capacity_guard=lambda *_args, **_kwargs: {"tier": "ok"},
            profile_builder=fake_profile,
        )
        self.assertEqual(payload["classification_counts"]["verified_pair"], 1)
        fields, rows = supply.read_csv_preserving_fields(self.high_csv)
        self.assertEqual(fields, updater.FIELDS)
        self.assertEqual(rows[0]["链接验证状态"], "verified_pair")
        records = supply.load_results(self.state_root / "results.jsonl")
        self.assertTrue(records[0]["csv_committed"])

    def test_failed_candidates_never_pollute_csv(self) -> None:
        bvid = "BV1234567890"
        write_csv(self.high_csv, [csv_row(bvid)])
        before = self.high_csv.read_bytes()
        bad = self.archive("no-assets.zip", {"readme.txt": b"no source"})
        candidate_path = self.root / "candidate.json"
        candidate_path.write_text(json.dumps(self.candidate(bvid, bad)), encoding="utf-8")
        payload = supply.run_supply(
            high_quali_csv=self.high_csv,
            candidate_paths=[candidate_path],
            state_root=self.state_root,
            start_row=1,
            limit=1,
            max_download_bytes=1024,
            max_archive_bytes=1024,
            max_archive_members=100,
            timeout=1,
            video_probe=fake_video_probe,
            profile_builder=fake_profile,
        )
        self.assertEqual(payload["classification_counts"]["non_asset_resource"], 1)
        self.assertEqual(self.high_csv.read_bytes(), before)

    def test_checkpoint_resume_avoids_repeat_verification_and_csv_write(self) -> None:
        bvid = "BV1234567890"
        # Exercise candidate-only append followed by resume after the authority
        # row now exists; the expected discovery bookkeeping must not invalidate
        # the expensive evidence fingerprint.
        write_csv(self.high_csv, [])
        archive = self.archive("source.zip", {"scene.blend": b"BLENDER"})
        candidate_path = self.root / "candidate.json"
        candidate_path.write_text(json.dumps(self.candidate(bvid, archive)), encoding="utf-8")
        calls = 0

        def probe(path: Path) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return fake_video_probe(path)

        kwargs = dict(
            high_quali_csv=self.high_csv,
            candidate_paths=[candidate_path],
            state_root=self.state_root,
            start_row=1,
            limit=1,
            max_download_bytes=1024,
            max_archive_bytes=1024,
            max_archive_members=100,
            timeout=1,
            video_probe=probe,
            capacity_guard=lambda *_args, **_kwargs: {"tier": "ok"},
            profile_builder=fake_profile,
        )
        first = supply.run_supply(**kwargs)
        csv_after_first = self.high_csv.read_bytes()
        second = supply.run_supply(**kwargs)
        self.assertEqual(first["processed_count"], 1)
        self.assertEqual(second["processed_count"], 0)
        self.assertEqual(second["resumed_count"], 1)
        self.assertEqual(calls, 1)
        self.assertEqual(self.high_csv.read_bytes(), csv_after_first)

    def test_registry_then_csv_failures_recover_idempotently(self) -> None:
        def run_kwargs(bvid: str, state_root: Path, candidate_path: Path) -> dict[str, object]:
            return {
                "high_quali_csv": self.high_csv,
                "candidate_paths": [candidate_path],
                "state_root": state_root,
                "start_row": 1,
                "limit": 1,
                "max_download_bytes": 1024,
                "max_archive_bytes": 1024,
                "max_archive_members": 100,
                "timeout": 1,
                "video_probe": fake_video_probe,
                "capacity_guard": lambda *_args, **_kwargs: {"tier": "ok"},
                "profile_builder": fake_profile,
            }

        first_bvid = "BV1234567890"
        write_csv(self.high_csv, [csv_row(first_bvid)])
        archive = self.archive("recovery-source.zip", {"scene.blend": b"BLENDER"})
        candidate_path = self.root / "recovery-candidate.json"
        candidate_path.write_text(
            json.dumps(self.candidate(first_bvid, archive)), encoding="utf-8"
        )
        before = self.high_csv.read_bytes()
        first_state = self.root / "registry-failure"
        with mock.patch.object(
            supply,
            "commit_verified_registry",
            side_effect=supply.SupplyError("injected registry failure"),
        ):
            with self.assertRaisesRegex(supply.SupplyError, "registry failure"):
                supply.run_supply(**run_kwargs(first_bvid, first_state, candidate_path))
        self.assertEqual(self.high_csv.read_bytes(), before)
        self.assertFalse((first_state / "verified_source_registry.jsonl").exists())
        recovered = supply.run_supply(**run_kwargs(first_bvid, first_state, candidate_path))
        self.assertEqual(recovered["classification_counts"]["verified_pair"], 1)

        second_bvid = "BVABCDEFGHIJ"
        write_csv(self.high_csv, [csv_row(second_bvid)])
        second_candidate = self.root / "csv-failure-candidate.json"
        second_candidate.write_text(
            json.dumps(self.candidate(second_bvid, archive)), encoding="utf-8"
        )
        second_state = self.root / "csv-failure"
        before = self.high_csv.read_bytes()
        with mock.patch.object(
            supply.highqal,
            "discover_verified_pairs",
            side_effect=supply.highqal.HighqalDataError("injected CSV failure"),
        ):
            with self.assertRaisesRegex(supply.highqal.HighqalDataError, "CSV failure"):
                supply.run_supply(**run_kwargs(second_bvid, second_state, second_candidate))
        self.assertEqual(self.high_csv.read_bytes(), before)
        self.assertEqual(
            len(supply.load_verified_registry(second_state / "verified_source_registry.jsonl")),
            1,
        )
        task1 = self.root / "empty-task1.csv"
        write_csv(task1, [], fields=[
            "asset_id", "status", "model_file", "视频链接", "标题", "分类", "素材链接", "failure_category"
        ])
        highqal_root = self.root / "half-commit-highqal"
        video_root = self.root / "half-commit-videos"
        highqal_root.mkdir()
        video_root.mkdir()
        half_committed, half_quarantine = supply.highqal.build_wave_manifest(
            task1_status=task1,
            high_quali_csv=self.high_csv,
            highqal_root=highqal_root,
            video_root=video_root,
            verified_registry=second_state / "verified_source_registry.jsonl",
            knowledge_generation="half-commit",
            profile_builder=fake_profile,
        )
        self.assertEqual(half_committed["items"], [])
        self.assertEqual(
            half_quarantine["items"][0]["reason"],
            "verified_registry_waiting_for_csv_verified_pair",
        )
        recovered = supply.run_supply(
            **run_kwargs(second_bvid, second_state, second_candidate)
        )
        self.assertEqual(recovered["classification_counts"]["verified_pair"], 1)
        _fields, rows = supply.read_csv_preserving_fields(self.high_csv)
        self.assertEqual(rows[0]["链接验证状态"], "verified_pair")
        committed, committed_quarantine = supply.highqal.build_wave_manifest(
            task1_status=task1,
            high_quali_csv=self.high_csv,
            highqal_root=highqal_root,
            video_root=video_root,
            verified_registry=second_state / "verified_source_registry.jsonl",
            knowledge_generation="committed",
            profile_builder=fake_profile,
        )
        self.assertEqual(len(committed["items"]), 1)
        self.assertEqual(committed_quarantine["items"], [])

    def test_status_is_read_only_and_reports_counts(self) -> None:
        write_csv(self.high_csv, [csv_row("BV1234567890")])
        self.state_root.mkdir()
        supply.atomic_write_jsonl(self.state_root / "results.jsonl", [{
            "schema": supply.RESULT_SCHEMA,
            "bvid": "BV1234567890",
            "classification": "video_unavailable",
        }])
        before = self.high_csv.read_bytes()
        payload = supply.status_payload(state_root=self.state_root, high_quali_csv=self.high_csv)
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["classification_counts"]["video_unavailable"], 1)
        self.assertEqual(self.high_csv.read_bytes(), before)

    def test_persistent_supply_logs_redact_url_tokens_and_extract_codes(self) -> None:
        bvid = "BV1234567890"
        row = csv_row(
            bvid,
            source_url="https://example.test/source.zip?token=QUERY_SECRET",
        )
        row["提取码"] = "CODE_SECRET"
        write_csv(self.high_csv, [row])
        archive = self.archive("secret-source.zip", {"scene.blend": b"BLENDER"})
        candidate = self.candidate(bvid, archive)
        candidate["素材链接"] = row["素材链接"]
        candidate["提取码"] = row["提取码"]
        candidate_path = self.root / "secret-candidate.json"
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        supply.run_supply(
            high_quali_csv=self.high_csv,
            candidate_paths=[candidate_path],
            state_root=self.state_root,
            start_row=1,
            limit=1,
            max_download_bytes=1024,
            max_archive_bytes=1024,
            max_archive_members=100,
            timeout=1,
            video_probe=fake_video_probe,
            capacity_guard=lambda *_args, **_kwargs: {"tier": "ok"},
            profile_builder=fake_profile,
        )
        persisted = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.state_root.rglob("*")
            if path.is_file() and path.suffix in {".json", ".jsonl"}
        )
        self.assertNotIn("QUERY_SECRET", persisted)
        self.assertNotIn("CODE_SECRET", persisted)
        self.assertIn("<redacted>", persisted)

    def test_public_url_rejects_private_dns_and_private_redirect(self) -> None:
        def private_resolver(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
            return [(2, 1, 6, "", ("127.0.0.1", 80))]

        with self.assertRaisesRegex(supply.HttpEvidenceError, "non-public"):
            supply.resolve_public_addresses("example.test", 80, resolver=private_resolver)

        connections = 0

        class RedirectResponse:
            status = 302
            headers = {"Location": "http://127.0.0.1/private"}

            def close(self) -> None:
                return

        class RedirectConnection:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                nonlocal connections
                connections += 1

            def request(self, *_args: object, **_kwargs: object) -> None:
                return

            def getresponse(self) -> RedirectResponse:
                return RedirectResponse()

            def close(self) -> None:
                return

        def rebinding_resolver(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
            address = "93.184.216.34" if host == "public.example" else "127.0.0.1"
            return [(2, 1, 6, "", (address, 80))]

        with mock.patch.object(supply, "_PinnedHTTPConnection", RedirectConnection):
            with self.assertRaisesRegex(supply.HttpEvidenceError, "non-public"):
                supply.open_public_http(
                    supply.urllib.request.Request("http://public.example/start"),
                    timeout=1,
                    resolver=rebinding_resolver,
                )
        self.assertEqual(connections, 1)

    def test_head_probe_opens_exactly_one_response(self) -> None:
        calls = 0

        class Response:
            status = 200
            headers = {"Content-Type": "text/html", "Content-Length": "10"}
            url = "https://example.test/video"

            def getcode(self) -> int:
                return self.status

            def close(self) -> None:
                return

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return

        def opener(*_args: object, **_kwargs: object) -> Response:
            nonlocal calls
            calls += 1
            return Response()

        evidence = supply.probe_public_url(
            "https://example.test/video", timeout=1, opener=opener
        )
        self.assertEqual(evidence["method"], "HEAD")
        self.assertEqual(calls, 1)

    def test_capacity_warning_pause_boundary_and_unknown_fail_closed(self) -> None:
        gib = 1024**3
        required = 2 * gib

        def capacity(free: int) -> supply.DiskCapacity:
            total = 2 * 1024 * gib
            return supply.DiskCapacity(total, total - free, free, "test")

        warning = supply.ensure_download_capacity(
            self.root,
            required_bytes=required,
            probe=lambda _path: capacity(700 * gib),
        )
        self.assertEqual(warning["tier"], "warning")
        boundary = supply.ensure_download_capacity(
            self.root,
            required_bytes=required,
            probe=lambda _path: capacity(502 * gib),
        )
        self.assertEqual(boundary["free_bytes"], 502 * gib)
        with self.assertRaisesRegex(supply.SupplyError, "500 GiB"):
            supply.ensure_download_capacity(
                self.root,
                required_bytes=required,
                probe=lambda _path: capacity(502 * gib - 1),
            )

        def unavailable(_path: object) -> supply.DiskCapacity:
            raise supply.DiskCapacityError("probe failed")

        with self.assertRaisesRegex(supply.SupplyError, "capacity is unknown"):
            supply.ensure_download_capacity(
                self.root, required_bytes=required, probe=unavailable
            )

    def test_raw_csv_run_can_download_and_ffprobe_both_sides(self) -> None:
        bvid = "BV1234567890"
        write_csv(self.high_csv, [csv_row(bvid)])
        source_calls = 0
        video_calls = 0

        def source_download(
            _url: str, *, destination_dir: Path, **_kwargs: object
        ) -> tuple[Path, dict[str, object]]:
            nonlocal source_calls
            source_calls += 1
            destination_dir.mkdir(parents=True, exist_ok=True)
            archive = destination_dir / "source.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("project/scene.blend", b"BLENDER")
                handle.writestr("textures/albedo.png", b"PNG")
            return archive, {"status": 200, "content_type": "application/zip"}

        def video_download(
            _url: str, *, destination_dir: Path, **_kwargs: object
        ) -> tuple[Path, dict[str, object]]:
            nonlocal video_calls
            video_calls += 1
            destination_dir.mkdir(parents=True, exist_ok=True)
            video = destination_dir / "reference.mp4"
            video.write_bytes(b"video-evidence" * 100)
            return video, {"status": 200, "method": "test_public_video"}

        payload = supply.run_supply(
            high_quali_csv=self.high_csv,
            candidate_paths=[],
            state_root=self.state_root,
            start_row=1,
            limit=1,
            max_download_bytes=1024 * 1024,
            max_archive_bytes=1024 * 1024,
            max_archive_members=100,
            timeout=1,
            video_probe=fake_video_probe,
            public_probe=lambda *_args, **_kwargs: {"status": 200, "method": "HEAD"},
            source_downloader=source_download,
            video_downloader=video_download,
            capacity_guard=lambda *_args, **_kwargs: {"tier": "ok", "source": "test"},
            profile_builder=fake_profile,
        )
        self.assertEqual(payload["classification_counts"]["verified_pair"], 1)
        self.assertEqual((source_calls, video_calls), (1, 1))
        _fields, rows = supply.read_csv_preserving_fields(self.high_csv)
        self.assertEqual(rows[0]["链接验证状态"], "verified_pair")

        registry_path = self.state_root / "verified_source_registry.jsonl"
        registry = supply.load_verified_registry(registry_path)
        self.assertEqual(len(registry), 1)
        self.assertTrue(Path(registry[0]["model_file"]).is_file())
        self.assertTrue(Path(registry[0]["reference_video"]).is_file())
        self.assertNotIn("提取码", registry[0])

        task1 = self.root / "task1.csv"
        write_csv(task1, [], fields=[
            "asset_id", "status", "model_file", "视频链接", "标题", "分类", "素材链接", "failure_category"
        ])
        highqal_root = self.root / "highqal-data"
        video_root = self.root / "videos"
        highqal_root.mkdir()
        video_root.mkdir()
        manifest, _quarantine = supply.highqal.build_wave_manifest(
            task1_status=task1,
            high_quali_csv=self.high_csv,
            highqal_root=highqal_root,
            video_root=video_root,
            verified_registry=registry_path,
            knowledge_generation="knowledge-test",
            wave=3,
            profile_builder=fake_profile,
        )
        self.assertEqual(len(manifest["items"]), 1)
        self.assertEqual(manifest["items"][0]["asset_id"], registry[0]["asset_id"])
        self.assertEqual(manifest["items"][0]["model_sha256"], registry[0]["model_sha256"])
        manifest_source_root = Path(manifest["items"][0]["source_root"])
        self.assertEqual(manifest_source_root, Path(registry[0]["source_root"]))
        self.assertTrue((manifest_source_root / "textures/albedo.png").is_file())
        self.assertIn("verified_source_registry", manifest["snapshots"])
        self.assertEqual(manifest["source_counts"]["verified_registry_projects"], 1)
        previous = self.root / "wave3.json"
        supply.highqal.atomic_write_json(previous, manifest)
        later, _ = supply.highqal.build_wave_manifest(
            task1_status=task1,
            high_quali_csv=self.high_csv,
            highqal_root=highqal_root,
            video_root=video_root,
            verified_registry=registry_path,
            knowledge_generation="knowledge-test",
            wave=4,
            previous_manifests=[previous],
            profile_builder=fake_profile,
        )
        self.assertEqual(len(later["items"]), 0)
        self.assertEqual(later["source_counts"]["excluded_previous_identity"], 1)

    def test_registry_bad_json_hash_schema_and_duplicate_fail_closed(self) -> None:
        registry = self.root / "registry.jsonl"
        registry.write_text("{broken\n", encoding="utf-8")
        with self.assertRaisesRegex(supply.SupplyError, "malformed"):
            supply.load_verified_registry(registry)
        with self.assertRaisesRegex(supply.highqal.HighqalDataError, "malformed"):
            supply.highqal.load_verified_source_registry(registry)

        model = self.root / "scene.blend"
        model.write_bytes(b"BLENDER")
        reference_video = self.root / "registry-reference.mp4"
        reference_video.write_bytes(b"video")
        record = {
            "schema": supply.REGISTRY_SCHEMA,
            "asset_id": "hq_BV1234567890_deadbeefdeadbeef",
            "bvid": "BV1234567890",
            "title": "title",
            "content_category": "",
            "model_file": str(model.resolve()),
            "model_format": ".blend",
            "model_sha256": supply.highqal.sha256_file(model),
            "model_size_bytes": model.stat().st_size,
            "source_root": str(self.root.resolve()),
            "source_package_sha256": "a" * 64,
            "source_relative_model": model.name,
            "reference_video": str(reference_video.resolve()),
            "reference_sha256": supply.highqal.sha256_file(reference_video),
            "reference_size_bytes": reference_video.stat().st_size,
            "video_url": "https://www.bilibili.com/video/BV1234567890/",
            "source_url": "https://example.test/source.zip",
        }
        record["registry_id"] = supply._registry_digest(record)
        supply.atomic_write_jsonl(registry, [record])
        self.assertEqual(len(supply.highqal.load_verified_source_registry(registry)), 1)

        corrupted = dict(record)
        corrupted["model_sha256"] = "0" * 64
        supply.atomic_write_jsonl(registry, [corrupted])
        with self.assertRaisesRegex(supply.highqal.HighqalDataError, "content hash differs"):
            supply.highqal.load_verified_source_registry(registry)

        unknown = dict(record)
        unknown["schema"] = "highqal-verified-source-registry.v0"
        unknown["registry_id"] = supply._registry_digest(unknown)
        supply.atomic_write_jsonl(registry, [unknown])
        with self.assertRaisesRegex(supply.highqal.HighqalDataError, "unsupported"):
            supply.highqal.load_verified_source_registry(registry)

        supply.atomic_write_jsonl(registry, [record, record])
        with self.assertRaisesRegex(supply.highqal.HighqalDataError, "duplicate"):
            supply.highqal.load_verified_source_registry(registry)

    def test_legacy_manifest_without_registry_snapshot_remains_valid(self) -> None:
        # Existing v2 manifests froze exactly the two original authority files.
        # Validation must remain backward compatible after the optional registry
        # snapshot was introduced.
        bvid = "BV1234567890"
        write_csv(self.high_csv, [csv_row(bvid)])
        task1 = self.root / "task1.csv"
        model = self.root / "legacy.blend"
        model.write_bytes(b"BLENDER")
        source_root = self.root / "linked_assets" / f"asset_{bvid}"
        source_root.mkdir(parents=True)
        legacy_model = source_root / "legacy.blend"
        legacy_model.write_bytes(model.read_bytes())
        write_csv(task1, [{
            "asset_id": f"asset_{bvid}",
            "status": "rendered",
            "model_file": str(legacy_model),
            "视频链接": f"https://www.bilibili.com/video/{bvid}/",
            "标题": "legacy",
            "分类": "static",
            "素材链接": "https://example.test/source.zip",
            "failure_category": "",
        }], fields=[
            "asset_id", "status", "model_file", "视频链接", "标题", "分类", "素材链接", "failure_category"
        ])
        highqal_root = self.root / "legacy-highqal"
        reference_dir = highqal_root / f"old_{bvid}"
        reference_dir.mkdir(parents=True)
        (reference_dir / "source.mp4").write_bytes(b"video")
        video_root = self.root / "legacy-videos"
        video_root.mkdir()
        manifest, _ = supply.highqal.build_wave_manifest(
            task1_status=task1,
            high_quali_csv=self.high_csv,
            highqal_root=highqal_root,
            video_root=video_root,
            verified_registry=None,
            knowledge_generation="legacy",
            profile_builder=fake_profile,
        )
        self.assertEqual(set(manifest["snapshots"]), {"task1_status", "high_quali_csv"})
        self.assertEqual(supply.highqal.validate_manifest(manifest)["generation"], manifest["generation"])

    def test_noncanonical_csv_columns_fail_closed_before_verified_mutation(self) -> None:
        bvid = "BV1234567890"
        fields = ["视频ID", "视频链接", "素材链接", "视频标题"]
        write_csv(self.high_csv, [{
            "视频ID": bvid,
            "视频链接": f"https://www.bilibili.com/video/{bvid}/",
            "素材链接": "https://example.test/source.zip",
            "视频标题": "title",
        }], fields=fields)
        before = self.high_csv.read_bytes()
        archive = self.archive("source.zip", {"scene.blend": b"BLENDER"})
        candidate_path = self.root / "candidate.json"
        candidate_path.write_text(json.dumps(self.candidate(bvid, archive)), encoding="utf-8")
        with self.assertRaisesRegex(supply.SupplyError, "column order"):
            supply.run_supply(
                high_quali_csv=self.high_csv,
                candidate_paths=[candidate_path],
                state_root=self.state_root,
                start_row=1,
                limit=1,
                max_download_bytes=1024,
                max_archive_bytes=1024,
                max_archive_members=100,
                timeout=1,
                video_probe=fake_video_probe,
                capacity_guard=lambda *_args, **_kwargs: {"tier": "ok"},
                profile_builder=fake_profile,
            )
        self.assertEqual(self.high_csv.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
