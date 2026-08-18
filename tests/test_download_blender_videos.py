from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "blender" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import download_blender_videos as downloader  # noqa: E402


class FakeResponse:
    def __init__(self, payload=b"", *, status=200, headers=None, reads=None):
        self.payload = payload
        self.status = status
        self.headers = headers or {}
        self.reads = list(reads) if reads is not None else None
        self._read_once = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self, _size=-1):
        if self.reads is not None:
            if not self.reads:
                return b""
            value = self.reads.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        if self._read_once:
            return b""
        self._read_once = True
        return self.payload


def public_view(*, bvid="BV1234567890"):
    return {
        "code": 0,
        "data": {
            "bvid": bvid,
            "title": "Public Scene",
            "owner": {"name": "Artist"},
            "rights": {"pay": 0, "ugc_pay": 0, "area_limit": 0},
            "pages": [{"cid": 2468, "part": "Public Scene"}],
        },
    }


def public_playurl():
    return {
        "code": 0,
        "data": {
            "is_preview": 0,
            "dash": {
                "video": [
                    {
                        "id": 80,
                        "width": 1920,
                        "height": 1080,
                        "bandwidth": 1000,
                        "baseUrl": "https://cdn.test/video.m4s",
                    }
                ],
                "audio": [
                    {
                        "id": 30280,
                        "bandwidth": 192000,
                        "baseUrl": "https://cdn.test/audio.m4s",
                    }
                ],
            },
        },
    }


def row():
    return {
        "平台": "Bilibili",
        "视频ID": "BV1234567890",
        "视频链接": "https://www.bilibili.com/video/BV1234567890/",
        "视频标题": "Public Scene",
    }


def args(**overrides):
    values = {
        "retries": 2,
        "target_subdir": None,
        "ffmpeg_location": None,
        "job_name": "wave2",
        "no_archive": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class PublicBilibiliFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def successful_runner(command, **_kwargs):
        executable = Path(command[0]).name
        if executable == "ffmpeg":
            Path(command[-1]).write_bytes(b"merged-mp4")
            return subprocess.CompletedProcess(command, 0, "", "")
        if executable == "ffprobe":
            payload = {
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                "format": {"duration": "12.5"},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        raise AssertionError(command)

    def public_opener(self, request, **_kwargs):
        url = request.full_url
        if url.startswith(downloader.BILIBILI_VIEW_API):
            return FakeResponse(json.dumps(public_view()).encode())
        if url.startswith(downloader.BILIBILI_PLAYURL_API):
            return FakeResponse(json.dumps(public_playurl()).encode())
        if url == "https://cdn.test/video.m4s":
            return FakeResponse(reads=[b"video", b""])
        if url == "https://cdn.test/audio.m4s":
            return FakeResponse(reads=[b"audio", b""])
        raise AssertionError(url)

    def test_412_or_extract_block_enables_only_public_bilibili_fallback(self):
        self.assertTrue(
            downloader.should_try_bilibili_public_fallback(
                row(), RuntimeError("HTTP Error 412: Precondition Failed")
            )
        )
        self.assertTrue(
            downloader.should_try_bilibili_public_fallback(
                row(), RuntimeError("Unable to extract initial state")
            )
        )
        self.assertFalse(
            downloader.should_try_bilibili_public_fallback(
                row(), RuntimeError("Login required: this is a private video")
            )
        )
        youtube = dict(row(), **{"平台": "YouTube", "视频链接": "https://youtube.test/BV1234567890"})
        self.assertFalse(
            downloader.should_try_bilibili_public_fallback(
                youtube, RuntimeError("HTTP Error 412")
            )
        )

    def test_full_fallback_uses_dash_merges_probes_publishes_and_archives(self):
        output = downloader.download_bilibili_public_fallback(
            row=row(),
            args=args(),
            output_root=self.root,
            opener=self.public_opener,
            runner=self.successful_runner,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(
            output,
            self.root / "Bilibili" / "Artist" / "Public Scene [BV1234567890].mp4",
        )
        self.assertEqual(output.read_bytes(), b"merged-mp4")
        archive = self.root / "_download_archive_wave2.txt"
        self.assertEqual(archive.read_text().splitlines(), ["bilibili BV1234567890"])
        self.assertEqual(list((self.root / "_tmp").glob("*.part")), [])

    def test_main_routes_yt_dlp_412_through_fallback_before_archive_commit(self):
        fake_args = argparse.Namespace(
            input=["ignored.csv"],
            output_root=str(self.root),
            target_subdir=None,
            job_name="wave2",
            limit=0,
            start_row=1,
            platform=None,
            relevance=None,
            sort_by_duration=False,
            shortest_per_platform=0,
            dry_run=False,
            discard_output=False,
            no_archive=False,
            retries=2,
            ffmpeg_location=None,
            host_ip=None,
        )
        output = self.root / "Bilibili" / "Artist" / "Public Scene [BV1234567890].mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"valid")

        class YoutubeDL:
            def __init__(self, _options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def download(self, _urls):
                raise RuntimeError("HTTP Error 412: Precondition Failed")

        def fallback_side_effect(**kwargs):
            kwargs["before_archive"](output)
            downloader._append_archive_idempotent(
                self.root / "_download_archive_wave2.txt", "BV1234567890"
            )
            return output

        with (
            mock.patch.object(downloader, "parse_args", return_value=fake_args),
            mock.patch.object(downloader, "resolve_inputs", return_value=[Path("ignored.csv")]),
            mock.patch.object(downloader, "load_rows", return_value=[row()]),
            mock.patch.object(downloader, "filter_rows", side_effect=lambda rows, _args: rows),
            mock.patch.object(downloader, "order_and_select_rows", side_effect=lambda rows, _args: rows),
            mock.patch.object(downloader, "validate_output_root", return_value=self.root),
            mock.patch.object(downloader, "install_static_host_resolver", return_value={}),
            mock.patch.object(downloader, "build_yt_dlp_options", return_value={}),
            mock.patch.object(
                downloader,
                "download_bilibili_public_fallback",
                side_effect=fallback_side_effect,
            ) as fallback,
            mock.patch.dict(sys.modules, {"yt_dlp": types.SimpleNamespace(YoutubeDL=YoutubeDL)}),
        ):
            self.assertEqual(downloader.main(), 0)

        self.assertTrue(callable(fallback.call_args.kwargs["before_archive"]))
        self.assertEqual(
            (self.root / "_download_archive_wave2.txt").read_text().splitlines(),
            ["bilibili BV1234567890"],
        )
        with (self.root / "download_results_wave2.csv").open(encoding="utf-8-sig") as handle:
            self.assertEqual(len(handle.read().splitlines()), 2)

    def test_range_resume_keeps_partial_bytes_and_requests_exact_offset(self):
        requests = []
        responses = [
            FakeResponse(status=200, reads=[b"abc", OSError("connection reset")]),
            FakeResponse(
                status=206,
                headers={"Content-Range": "bytes 3-5/6"},
                reads=[b"def", b""],
            ),
        ]

        def opener(request, **_kwargs):
            requests.append(request)
            return responses.pop(0)

        part = self.root / "_tmp" / "video.part"
        downloader.download_url_with_resume(
            ["https://cdn.test/video.m4s"],
            part,
            referer="https://www.bilibili.com/video/BV1234567890/",
            retries=2,
            opener=opener,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(part.read_bytes(), b"abcdef")
        self.assertNotIn("Range", dict(requests[0].header_items()))
        self.assertEqual(dict(requests[1].header_items()).get("Range"), "bytes=3-")

    def test_view_api_failure_is_fail_closed_and_does_not_consume_archive(self):
        archive = self.root / "_download_archive_wave2.txt"
        archive.write_text("BiliBili OLD\n")

        def opener(request, **_kwargs):
            self.assertTrue(request.full_url.startswith(downloader.BILIBILI_VIEW_API))
            return FakeResponse(json.dumps({"code": -404, "message": "not found"}).encode())

        with self.assertRaisesRegex(downloader.BilibiliFallbackError, "view_api_denied"):
            downloader.download_bilibili_public_fallback(
                row=row(),
                args=args(),
                output_root=self.root,
                opener=opener,
                runner=self.successful_runner,
                sleep_fn=lambda _seconds: None,
            )
        self.assertEqual(archive.read_text(), "BiliBili OLD\n")

    def test_paid_or_preview_video_is_rejected_before_media_download(self):
        paid = public_view()
        paid["data"]["rights"]["pay"] = 1

        def opener(request, **_kwargs):
            return FakeResponse(json.dumps(paid).encode())

        with self.assertRaisesRegex(downloader.BilibiliFallbackError, "public_access_denied"):
            downloader.resolve_bilibili_public_dash(
                row(), retries=1, opener=opener, sleep_fn=lambda _seconds: None
            )

    def test_api_json_stream_accepts_payload_larger_than_legacy_two_mib_limit(self):
        padding = "x" * (2 * 1024 * 1024 + 32)
        payload = json.dumps({"code": 0, "data": {"padding": padding}}).encode()

        decoded = downloader.fetch_bilibili_public_json(
            downloader.BILIBILI_VIEW_API,
            {"bvid": "BV1234567890"},
            referer="https://www.bilibili.com/video/BV1234567890/",
            retries=1,
            opener=lambda _request, **_kwargs: FakeResponse(payload),
            sleep_fn=lambda _seconds: None,
        )

        self.assertEqual(decoded["code"], 0)
        self.assertEqual(len(decoded["data"]["padding"]), len(padding))

    def test_api_json_stream_remains_strictly_bounded(self):
        with self.assertRaisesRegex(
            downloader.BilibiliFallbackError, "api_response_too_large"
        ):
            downloader.read_bounded_response(
                FakeResponse(b"x" * 33, headers={"Content-Length": "33"}),
                maximum_bytes=32,
                chunk_bytes=8,
            )

    def test_only_explicit_permanent_access_failures_leave_retry_path(self):
        self.assertEqual(
            downloader.classify_permanent_download_error(
                downloader.BilibiliFallbackError(
                    "public_access_denied", "paid or creator-exclusive video"
                )
            ),
            "auth_required",
        )
        self.assertEqual(
            downloader.classify_permanent_download_error(
                RuntimeError("This video is not available in your country")
            ),
            "region_locked",
        )
        self.assertEqual(
            downloader.classify_permanent_download_error(
                RuntimeError("video has been deleted")
            ),
            "video_unavailable",
        )
        for message in (
            "HTTP Error 412: Precondition Failed",
            "connection timed out",
            "api_response_too_large",
            "unable to extract initial state",
        ):
            self.assertEqual(
                downloader.classify_permanent_download_error(RuntimeError(message)),
                "",
            )

    def test_pending_queue_update_is_atomic_and_never_drops_retryable_rows(self):
        queue = self.root / "wave2.csv"
        fields = ["平台", "视频ID", "视频链接", "视频标题"]
        first = row()
        second = dict(
            row(),
            **{
                "视频ID": "BVABCDEFGHIJ",
                "视频链接": "https://www.bilibili.com/video/BVABCDEFGHIJ/",
                "视频标题": "Retry Me",
            },
        )
        with queue.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows([first, second])

        retained = downloader.rewrite_pending_input_atomic(
            queue, {first["视频链接"]}
        )

        self.assertEqual(retained, 1)
        with queue.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows, [{key: second[key] for key in fields}])
        self.assertFalse(list(self.root.glob(".wave2.csv.*.tmp")))

    def test_main_records_paid_video_terminal_and_removes_it_from_pending_queue(self):
        queue = self.root / "wave2.csv"
        fields = ["平台", "视频ID", "视频链接", "视频标题"]
        with queue.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerow(row())
        fake_args = argparse.Namespace(
            input=[str(queue)],
            output_root=str(self.root),
            target_subdir=None,
            job_name="wave2",
            limit=0,
            start_row=1,
            platform=None,
            relevance=None,
            sort_by_duration=False,
            shortest_per_platform=0,
            dry_run=False,
            discard_output=False,
            no_archive=False,
            rewrite_pending_input=True,
            retries=2,
            ffmpeg_location=None,
            host_ip=None,
        )

        class YoutubeDL:
            calls = 0

            def __init__(self, _options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def download(self, _urls):
                type(self).calls += 1
                raise RuntimeError("This video is paid or creator-exclusive")

        with (
            mock.patch.object(downloader, "parse_args", return_value=fake_args),
            mock.patch.object(downloader, "resolve_inputs", return_value=[queue]),
            mock.patch.object(downloader, "load_rows", return_value=[row()]),
            mock.patch.object(downloader, "filter_rows", side_effect=lambda rows, _args: rows),
            mock.patch.object(downloader, "order_and_select_rows", side_effect=lambda rows, _args: rows),
            mock.patch.object(downloader, "validate_output_root", return_value=self.root),
            mock.patch.object(downloader, "install_static_host_resolver", return_value={}),
            mock.patch.object(downloader, "build_yt_dlp_options", return_value={}),
            mock.patch.dict(sys.modules, {"yt_dlp": types.SimpleNamespace(YoutubeDL=YoutubeDL)}),
        ):
            self.assertEqual(downloader.main(), 0)

        self.assertEqual(YoutubeDL.calls, 1)
        with queue.open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(list(csv.DictReader(handle)), [])
        ledger = self.root / "download_results_wave2.csv"
        with ledger.open(encoding="utf-8-sig", newline="") as handle:
            results = list(csv.DictReader(handle))
        self.assertEqual(results[-1]["状态"], "auth_required")

    def test_ffprobe_failure_keeps_parts_and_does_not_consume_archive(self):
        archive = self.root / "_download_archive_wave2.txt"
        archive.write_text("BiliBili OLD\n")

        def runner(command, **_kwargs):
            if Path(command[0]).name == "ffmpeg":
                Path(command[-1]).write_bytes(b"bad-merged")
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 1, "", "decode error")

        with self.assertRaisesRegex(downloader.BilibiliFallbackError, "ffprobe_failed"):
            downloader.download_bilibili_public_fallback(
                row=row(),
                args=args(),
                output_root=self.root,
                opener=self.public_opener,
                runner=runner,
                sleep_fn=lambda _seconds: None,
            )
        self.assertEqual(archive.read_text(), "BiliBili OLD\n")
        self.assertEqual(len(list((self.root / "_tmp").glob("*.video.part"))), 1)
        self.assertEqual(len(list((self.root / "_tmp").glob("*.audio.part"))), 1)
        self.assertFalse(
            (self.root / "Bilibili" / "Artist" / "Public Scene [BV1234567890].mp4").exists()
        )

    def test_archive_and_success_result_are_idempotent(self):
        archive = self.root / "archive.txt"
        archive.write_text("BiliBili OLD\n")
        downloader._append_archive_idempotent(archive, "BV1234567890")
        downloader._append_archive_idempotent(archive, "BV1234567890")
        self.assertEqual(
            archive.read_text().splitlines(),
            ["BiliBili OLD", "bilibili BV1234567890"],
        )

        results = self.root / "results.csv"
        downloader.ensure_results_header(results)
        downloader.append_result(results, row(), "ok", "/output.mp4", "", "now")
        downloader.append_result(results, row(), "ok", "/output.mp4", "", "later")
        with results.open(encoding="utf-8-sig") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
