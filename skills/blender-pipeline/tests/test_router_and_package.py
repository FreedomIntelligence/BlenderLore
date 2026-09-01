from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_package as validator


class RouterContractTests(unittest.TestCase):
    def test_router_is_concise_and_resolves_all_routes(self) -> None:
        path = PACKAGE_ROOT / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        metadata = validator.frontmatter(text)
        self.assertEqual(metadata["name"], "blender-pipeline")
        self.assertLessEqual(len(text.splitlines()), 80)
        self.assertTrue((PACKAGE_ROOT / "generation/SKILL.md").is_file())
        self.assertTrue((PACKAGE_ROOT / "editing/SKILL.md").is_file())
        self.assertTrue((PACKAGE_ROOT / "reproduction/SKILL.md").is_file())
        self.assertFalse(validator.check_links(PACKAGE_ROOT))

    def test_package_contract_is_self_consistent(self) -> None:
        issues = validator.validate(PACKAGE_ROOT)
        self.assertEqual([], issues, "\n".join(str(issue) for issue in issues))

    def test_sensitive_and_broken_link_checks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            private_path = "/" + "Volumes" + "/private-store/input.blend"
            (root / "sample.py").write_text(
                f"SOURCE = {private_path!r}\n",
                encoding="utf-8",
            )
            (root / "guide.md").write_text(
                "[missing reference](missing.md)\n",
                encoding="utf-8",
            )
            sensitive = validator.check_sensitive_paths(root)
            links = validator.check_links(root)
            self.assertEqual(
                ["mounted-volume path"], [issue.message for issue in sensitive]
            )
            self.assertEqual(1, len(links))
            self.assertIn("broken local link", links[0].message)

    def test_local_import_check_detects_missing_pipeline_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "generation/scripts"
            scripts.mkdir(parents=True)
            (scripts / "entry.py").write_text(
                "from video_replay_missing import run\n",
                encoding="utf-8",
            )
            issues = validator.check_imports(root)
            self.assertEqual(1, len(issues))
            self.assertIn("unresolved generation import", issues[0].message)


if __name__ == "__main__":
    unittest.main()
