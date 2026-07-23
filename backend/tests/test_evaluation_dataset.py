"""离线评测数据集的结构与覆盖范围回归测试。"""

from __future__ import annotations

import json
import sys
import unittest
from collections import Counter
from pathlib import Path


DATASET_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "dataset.jsonl"
BACKEND_PATH = DATASET_PATH.parents[1]
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from evaluation.evaluate import evaluation_split  # noqa: E402
SUPPORTED_CATEGORIES = {
    "refund",
    "billing",
    "technical",
    "product_question",
    "complaint",
}


class EvaluationDatasetTest(unittest.TestCase):
    """防止评测样本在后续编辑中丢失、重复或出现非法标签。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = [
            json.loads(line)
            for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_dataset_has_unique_ids_and_expected_size(self) -> None:
        case_ids = [case["id"] for case in self.cases]
        self.assertEqual(200, len(case_ids))
        self.assertEqual(len(case_ids), len(set(case_ids)))

    def test_dataset_balances_language_and_support_intent(self) -> None:
        languages = Counter(case["language"] for case in self.cases)
        support_counts = Counter(case["expected_support"] for case in self.cases)
        self.assertEqual({"zh": 100, "en": 100}, dict(languages))
        self.assertEqual(140, support_counts[True])
        self.assertEqual(60, support_counts[False])

    def test_each_support_category_has_chinese_and_english_cases(self) -> None:
        support_cases = [case for case in self.cases if case["expected_support"]]
        categories = Counter(case["expected_category"] for case in support_cases)
        self.assertEqual({category: 28 for category in SUPPORTED_CATEGORIES}, dict(categories))

        coverage = {
            (case["expected_category"], case["language"])
            for case in support_cases
        }
        expected_coverage = {
            (category, language)
            for category in SUPPORTED_CATEGORIES
            for language in ("zh", "en")
        }
        self.assertEqual(expected_coverage, coverage)

    def test_support_cases_have_retrieval_labels(self) -> None:
        for case in self.cases:
            with self.subTest(case_id=case["id"]):
                if case["expected_support"]:
                    self.assertIn(case["expected_risk"], {"low", "medium", "high"})
                    self.assertTrue(case["expected_sources"])
                else:
                    self.assertEqual("other", case["expected_category"])
                    self.assertEqual([], case["expected_sources"])

    def test_calibration_and_holdout_split_is_stable_and_populated(self) -> None:
        first_pass = {
            case["id"]: evaluation_split(case["id"])
            for case in self.cases
        }
        second_pass = {
            case["id"]: evaluation_split(case["id"])
            for case in reversed(self.cases)
        }
        self.assertEqual(first_pass, second_pass)
        split_counts = Counter(first_pass.values())
        self.assertGreater(split_counts["calibration"], 120)
        self.assertGreater(split_counts["holdout"], 40)


if __name__ == "__main__":
    unittest.main()
