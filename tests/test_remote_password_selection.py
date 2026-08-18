from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "blender/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_total_asset_render_worker as worker


class RemotePasswordSelectionTests(unittest.TestCase):
    def test_port_specific_secret_overrides_legacy_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = root / "legacy"
            rotated = root / "rotated"
            with mock.patch.object(worker, "PASSWORD_FILE", legacy), mock.patch.dict(
                os.environ,
                {"REMOTE_GPU_PASSWORD_FILE_30808": str(rotated)},
                clear=False,
            ):
                self.assertEqual(worker.password_file_for_port(30773), legacy)
                self.assertEqual(worker.password_file_for_port(30808), rotated)
                self.assertEqual(worker.Remote(30808, 0).password_file, rotated)


if __name__ == "__main__":
    unittest.main()
