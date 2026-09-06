from __future__ import annotations

import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tutorial_extraction_core as core


class AudioStreamPreflightTests(unittest.TestCase):
    def test_no_audio_never_loads_asr_model(self):
        backend = types.SimpleNamespace(
            transcribe=Mock(side_effect=AssertionError("ASR must not load"))
        )
        with (
            patch.object(
                core,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            patch.dict(sys.modules, {"mlx_whisper": backend}),
        ):
            result = core.local_asr(Path("caption-only.mp4"))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("no audio stream", result.warning)
        self.assertEqual(result.attempted_sources, ["audio_stream_probe"])
        backend.transcribe.assert_not_called()

    def test_audio_probe_failure_is_explicit_without_download(self):
        backend = types.SimpleNamespace(
            transcribe=Mock(side_effect=AssertionError("ASR must not load"))
        )
        with (
            patch.object(
                core, "run_command", side_effect=core.ExtractionError("probe failed")
            ),
            patch.dict(sys.modules, {"mlx_whisper": backend}),
        ):
            result = core.local_asr(Path("unreadable.mp4"))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("Could not inspect", result.warning)
        backend.transcribe.assert_not_called()

    def test_audio_track_still_routes_to_existing_asr(self):
        backend = types.SimpleNamespace(
            transcribe=Mock(
                return_value={
                    "segments": [
                        {
                            "start": 0,
                            "end": 6,
                            "text": "Start by adding a cube to the empty scene.",
                        }
                    ]
                }
            )
        )
        with (
            patch.object(
                core,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "1\n", ""),
            ),
            patch.dict(sys.modules, {"mlx_whisper": backend}),
        ):
            result = core.local_asr(Path("spoken.mp4"), language_hint="en")
        self.assertEqual(result.source, "local_asr:mlx-whisper")
        backend.transcribe.assert_called_once()
        self.assertEqual(backend.transcribe.call_args.kwargs["language"], "en")

    def test_invalid_language_rejected_before_probe(self):
        with patch.object(core, "run_command") as probe:
            with self.assertRaises(ValueError):
                core.local_asr(Path("spoken.mp4"), language_hint="en;other")
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
