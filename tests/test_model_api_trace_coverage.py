import ast
import unittest
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRACED_MODEL_FUNCTIONS = {
    "blender/scripts/generate_rich_tutorial_chunks.py": {"call_model"},
    "blender/scripts/total_asset_pipeline.py": {"refine_ambiguous_with_llm"},
    "blender/scripts/run_video_strict_replay.py": {
        "request_code",
        "review_static_render",
    },
    "blender/legacy_compat/run_tutorial_replay_pipeline.py": {"request_model"},
    "blender/legacy_compat/run_favlist_5090_batch.py": {
        "api_preflight",
        "call_model",
    },
    "blender/legacy_compat/remote_favlist_asset_worker.py": {"call_model"},
}
NON_MODEL_POST_FUNCTIONS = {
    "blender/scripts/bilibili_quark_netdisk_downloader.py": {"download_infos"},
}


class ModelApiTraceCoverageTests(unittest.TestCase):
    def test_every_new_post_call_must_be_classified(self) -> None:
        discovered: dict[str, set[str]] = defaultdict(set)
        for path in (ROOT / "blender").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            functions = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
                if not isinstance(call.func, ast.Attribute) or call.func.attr != "post":
                    continue
                owners = [
                    function
                    for function in functions
                    if function.lineno <= call.lineno <= (function.end_lineno or call.lineno)
                ]
                self.assertTrue(owners, f"POST outside a function: {path}:{call.lineno}")
                owner = min(
                    owners,
                    key=lambda function: (function.end_lineno or function.lineno)
                    - function.lineno,
                )
                discovered[str(path.relative_to(ROOT))].add(owner.name)
        expected: dict[str, set[str]] = defaultdict(set)
        for mapping in (TRACED_MODEL_FUNCTIONS, NON_MODEL_POST_FUNCTIONS):
            for relative, names in mapping.items():
                expected[relative].update(names)
        self.assertEqual(dict(discovered), dict(expected))

    def test_every_model_post_path_records_request_response_and_exception(self) -> None:
        for relative, names in TRACED_MODEL_FUNCTIONS.items():
            source = (ROOT / relative).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
            functions = {
                node.name: ast.get_source_segment(source, node) or ""
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for name in names:
                with self.subTest(path=relative, function=name):
                    body = functions[name]
                    post_positions = [
                        position
                        for marker in ("requests.post(", "session.post(")
                        if (position := body.find(marker)) >= 0
                    ]
                    self.assertTrue(post_positions, "model function has no POST call")
                    start = body.find("start_api_trace(")
                    self.assertGreaterEqual(start, 0)
                    self.assertLess(start, min(post_positions))
                    self.assertIn("record_api_response(", body)
                    self.assertIn("record_api_exception(", body)


if __name__ == "__main__":
    unittest.main()
