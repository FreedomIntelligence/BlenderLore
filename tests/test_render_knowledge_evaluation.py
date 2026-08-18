from __future__ import annotations

import copy
import json
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

import evaluate_render_knowledge as evaluation


def passing_rows() -> tuple[list[dict], list[dict]]:
    samples = [
        {
            "schema_version": evaluation.SAMPLE_SCHEMA_VERSION,
            "sample_id": "dynamic-001",
            "truth": {
                "subject_family": "material",
                "motion_mechanisms": ["cloth_softbody"],
                "route": "dynamic",
                "acceptable_recipe_ids": ["dynamic_simulation"],
            },
            "flags": {
                "ood": False,
                "requires_output_contract": True,
                "repairable": True,
                "dynamic_quality": True,
                "efficiency": True,
            },
            "forbidden_recipe_ids": {
                "incompatible": ["cycles_only_wrong_version"],
                "deprecated": ["old_simulation"],
            },
            "baseline": {
                "strict_accepted": False,
                "visible_motion_pass": False,
                "dynamic_misroute": True,
                "gpu_minutes": 100.0,
                "repair_attempts": 10,
            },
        },
        {
            "schema_version": evaluation.SAMPLE_SCHEMA_VERSION,
            "sample_id": "ood-001",
            "truth": {
                "subject_family": "unknown",
                "motion_mechanisms": ["none"],
                "route": "unknown",
                "acceptable_recipe_ids": [],
            },
            "flags": {
                "ood": True,
                "requires_output_contract": False,
                "repairable": False,
                "dynamic_quality": False,
                "efficiency": False,
            },
            "forbidden_recipe_ids": {
                "incompatible": ["static_six_view"],
                "deprecated": ["old_generic"],
            },
            "baseline": {},
        },
    ]
    predictions = [
        {
            "schema_version": evaluation.PREDICTION_SCHEMA_VERSION,
            "sample_id": "dynamic-001",
            "prediction": {
                "subject_family": "material",
                "motion_mechanisms": ["cloth_softbody"],
                "route": "dynamic",
                "top3_recipe_ids": ["dynamic_simulation", "static_six_view"],
                "selected_recipe_id": "dynamic_simulation",
                "executable": True,
                "abstained": False,
            },
            "outcome": {
                "output_contract_pass": True,
                "strict_accepted": True,
                "visible_motion_pass": True,
                "dynamic_misroute": False,
                "gpu_minutes": 80.0,
                "repair_attempts": 7,
            },
        },
        {
            "schema_version": evaluation.PREDICTION_SCHEMA_VERSION,
            "sample_id": "ood-001",
            "prediction": {
                "subject_family": "unknown",
                "motion_mechanisms": ["none"],
                "route": "unknown",
                "top3_recipe_ids": [],
                "selected_recipe_id": "",
                "executable": False,
                "abstained": True,
            },
            "outcome": {},
        },
    ]
    return samples, predictions


class RenderKnowledgeEvaluationTests(unittest.TestCase):
    def test_all_declared_threshold_boundaries_are_inclusive(self) -> None:
        for name, (comparison, threshold) in evaluation.THRESHOLDS.items():
            with self.subTest(metric=name):
                self.assertEqual(evaluation.metric_value(name, threshold, count=1)["status"], "pass")
                failing_value = threshold - 0.0001 if comparison == ">=" else threshold + 0.0001
                self.assertEqual(evaluation.metric_value(name, failing_value, count=1)["status"], "fail")

    def test_complete_corpus_passes_all_thresholds(self) -> None:
        samples, predictions = passing_rows()
        report = evaluation.evaluate_rows(samples, predictions)
        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["evaluable"])
        self.assertTrue(all(gate["status"] == "pass" for gate in report["gates"].values()))
        self.assertAlmostEqual(report["metrics"]["subject_macro_f1"]["value"], 1.0)
        self.assertAlmostEqual(report["metrics"]["motion_macro_f1"]["value"], 1.0)
        self.assertAlmostEqual(report["metrics"]["route_accuracy"]["value"], 1.0)
        self.assertAlmostEqual(report["metrics"]["top3_recipe_recall"]["value"], 1.0)
        self.assertAlmostEqual(report["metrics"]["executable_precision"]["value"], 1.0)
        self.assertAlmostEqual(report["metrics"]["ood_abstain_rate"]["value"], 1.0)
        self.assertAlmostEqual(report["metrics"]["strict_accepted_improvement_pp"]["value"], 100.0)
        self.assertAlmostEqual(report["metrics"]["median_gpu_minutes_reduction"]["value"], 0.2)
        self.assertAlmostEqual(report["metrics"]["repair_attempt_reduction"]["value"], 0.3)
        self.assertAlmostEqual(report["metrics"]["p95_gpu_minutes_increase"]["value"], -0.2)

    def test_all_abstain_cannot_claim_executable_precision(self) -> None:
        samples, predictions = passing_rows()
        predictions[0]["prediction"]["executable"] = False
        predictions[0]["prediction"]["abstained"] = True
        predictions[0]["prediction"]["selected_recipe_id"] = ""
        report = evaluation.evaluate_rows(samples, predictions)
        metric = report["metrics"]["executable_precision"]
        self.assertEqual(metric["status"], "not_evaluable")
        self.assertIn("all-abstain", metric["reason"])
        self.assertEqual(report["status"], "fail")

    def test_missing_efficiency_data_is_not_evaluable_and_fails_closed(self) -> None:
        samples, predictions = passing_rows()
        del predictions[0]["outcome"]["gpu_minutes"]
        report = evaluation.evaluate_rows(samples, predictions)
        self.assertEqual(report["metrics"]["median_gpu_minutes_reduction"]["status"], "not_evaluable")
        self.assertEqual(report["metrics"]["p95_gpu_minutes_increase"]["status"], "not_evaluable")
        self.assertFalse(report["evaluable"])
        self.assertEqual(report["status"], "fail")

    def test_missing_required_cohort_flag_invalidates_input(self) -> None:
        samples, predictions = passing_rows()
        del samples[0]["flags"]["efficiency"]
        report = evaluation.evaluate_rows(samples, predictions)
        self.assertEqual(report["gates"]["input_coverage"]["status"], "fail")
        self.assertTrue(all(metric["status"] == "not_evaluable" for metric in report["metrics"].values()))
        self.assertEqual(report["status"], "fail")

    def test_contradictory_executable_and_abstained_flags_fail_input_gate(self) -> None:
        samples, predictions = passing_rows()
        predictions[0]["prediction"]["abstained"] = True
        report = evaluation.evaluate_rows(samples, predictions)
        self.assertEqual(report["gates"]["input_coverage"]["status"], "fail")
        self.assertTrue(any("cannot be executable and abstained" in issue for issue in report["issues"]))

    def test_negative_gpu_or_repair_values_are_not_evaluable(self) -> None:
        samples, predictions = passing_rows()
        predictions[0]["outcome"]["gpu_minutes"] = -1
        predictions[0]["outcome"]["repair_attempts"] = -1
        report = evaluation.evaluate_rows(samples, predictions)
        self.assertEqual(report["metrics"]["median_gpu_minutes_reduction"]["status"], "not_evaluable")
        self.assertEqual(report["metrics"]["repair_attempt_reduction"]["status"], "not_evaluable")
        self.assertEqual(report["status"], "fail")

    def test_missing_ood_cohort_is_not_evaluable(self) -> None:
        samples, predictions = passing_rows()
        report = evaluation.evaluate_rows(samples[:1], predictions[:1])
        self.assertEqual(report["metrics"]["ood_abstain_rate"]["status"], "not_evaluable")
        self.assertEqual(report["status"], "fail")

    def test_incompatible_and_deprecated_hits_fail_even_when_not_selected(self) -> None:
        samples, predictions = passing_rows()
        predictions[0]["prediction"]["top3_recipe_ids"] = [
            "dynamic_simulation",
            "cycles_only_wrong_version",
            "old_simulation",
        ]
        report = evaluation.evaluate_rows(samples, predictions)
        self.assertEqual(report["metrics"]["incompatible_recipe_hits"]["status"], "fail")
        self.assertEqual(report["metrics"]["deprecated_recipe_hits"]["status"], "fail")
        self.assertEqual(report["status"], "fail")

    def test_top3_recall_uses_prediction_order_not_sorted_membership(self) -> None:
        samples, predictions = passing_rows()
        predictions[0]["prediction"]["top3_recipe_ids"] = [
            "z_wrong",
            "y_wrong",
            "x_wrong",
            "dynamic_simulation",
        ]
        report = evaluation.evaluate_rows(samples, predictions)
        self.assertEqual(report["metrics"]["top3_recipe_recall"]["value"], 0.0)
        self.assertEqual(report["metrics"]["top3_recipe_recall"]["status"], "fail")

    def test_route_quality_and_output_contract_threshold_failures_are_reported(self) -> None:
        samples, predictions = passing_rows()
        predictions[0]["prediction"]["route"] = "static"
        predictions[0]["outcome"]["output_contract_pass"] = False
        predictions[0]["outcome"]["strict_accepted"] = False
        predictions[0]["outcome"]["visible_motion_pass"] = False
        predictions[0]["outcome"]["dynamic_misroute"] = True
        report = evaluation.evaluate_rows(samples, predictions)
        self.assertEqual(report["metrics"]["route_accuracy"]["status"], "fail")
        self.assertEqual(report["metrics"]["output_contract_rate"]["status"], "fail")
        self.assertEqual(report["metrics"]["strict_accepted_improvement_pp"]["status"], "fail")
        self.assertEqual(report["gates"]["quality"]["status"], "fail")
        self.assertEqual(report["status"], "fail")

    def test_zero_baseline_improvement_cannot_be_fabricated(self) -> None:
        samples, predictions = passing_rows()
        samples[0]["baseline"]["repair_attempts"] = 0
        predictions[0]["outcome"]["repair_attempts"] = 0
        samples[0]["baseline"]["dynamic_misroute"] = False
        predictions[0]["outcome"]["dynamic_misroute"] = False
        report = evaluation.evaluate_rows(samples, predictions)
        self.assertEqual(report["metrics"]["repair_attempt_reduction"]["status"], "not_evaluable")
        self.assertEqual(report["metrics"]["dynamic_misroute_reduction"]["status"], "not_evaluable")
        self.assertEqual(report["status"], "fail")

    def test_prediction_id_coverage_mismatch_invalidates_every_metric(self) -> None:
        samples, predictions = passing_rows()
        predictions.pop()
        report = evaluation.evaluate_rows(samples, predictions)
        self.assertEqual(report["gates"]["input_coverage"]["status"], "fail")
        self.assertIn("coverage mismatch", report["issues"][0])
        self.assertTrue(all(metric["status"] == "not_evaluable" for metric in report["metrics"].values()))

    def test_wrong_schema_and_duplicate_ids_fail_closed(self) -> None:
        samples, predictions = passing_rows()
        bad_schema = copy.deepcopy(samples)
        bad_schema[0]["schema_version"] = "render-knowledge-eval-sample-v99"
        report = evaluation.evaluate_rows(bad_schema, predictions)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["gates"]["input_coverage"]["status"], "fail")
        duplicate = samples + [copy.deepcopy(samples[0])]
        report = evaluation.evaluate_rows(duplicate, predictions)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("duplicate sample" in issue for issue in report["issues"]))

    def test_strict_jsonl_loader_rejects_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.jsonl"
            path.write_text('{"schema_version": "render-knowledge-eval-sample-v1"\n', encoding="utf-8")
            with self.assertRaises(evaluation.EvaluationInputError):
                evaluation.read_versioned_jsonl(path, evaluation.SAMPLE_SCHEMA_VERSION)

    def test_cli_writes_versioned_report_and_uses_exit_status(self) -> None:
        samples, predictions = passing_rows()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            samples_path = root / "samples.jsonl"
            predictions_path = root / "predictions.jsonl"
            output_path = root / "report.json"
            samples_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in samples), encoding="utf-8"
            )
            predictions_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in predictions), encoding="utf-8"
            )
            argv = [
                "evaluate_render_knowledge.py",
                "--samples",
                str(samples_path),
                "--predictions",
                str(predictions_path),
                "--output",
                str(output_path),
            ]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
                self.assertEqual(evaluation.main(), 0)
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["schema_version"], evaluation.REPORT_SCHEMA_VERSION)
            self.assertEqual(report["status"], "pass")

    def test_cli_returns_nonzero_for_failed_acceptance(self) -> None:
        samples, predictions = passing_rows()
        predictions[0]["outcome"]["output_contract_pass"] = False
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            samples_path = root / "samples.jsonl"
            predictions_path = root / "predictions.jsonl"
            samples_path.write_text(
                "".join(json.dumps(row) + "\n" for row in samples), encoding="utf-8"
            )
            predictions_path.write_text(
                "".join(json.dumps(row) + "\n" for row in predictions), encoding="utf-8"
            )
            argv = [
                "evaluate_render_knowledge.py",
                "--samples",
                str(samples_path),
                "--predictions",
                str(predictions_path),
            ]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(StringIO()):
                self.assertEqual(evaluation.main(), 2)


if __name__ == "__main__":
    unittest.main()
