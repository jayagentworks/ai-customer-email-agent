"""OCR 版面恢复、质量评分和关键字段保护测试。"""

from __future__ import annotations

import unittest

from app.document_processor import (
    OCRTextBlock,
    build_ocr_blocks,
    order_ocr_blocks,
    parse_tesseract_tsv,
    score_ocr_quality,
)
from app.ocr_repair_client import extract_protected_tokens, length_change_ratio


class OCRLayoutTest(unittest.TestCase):
    def test_tesseract_tsv_is_parsed_and_joined(self) -> None:
        tsv = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t10\t20\t30\t10\t93.0\t企业\n"
            "5\t1\t1\t1\t1\t2\t42\t20\t30\t10\t95.0\t政策\n"
            "5\t1\t1\t1\t2\t1\t10\t40\t60\t10\t90.0\tPolicy\n"
        )
        words = parse_tesseract_tsv(tsv)
        blocks = build_ocr_blocks(words)
        self.assertEqual(3, len(words))
        self.assertEqual(1, len(blocks))
        self.assertEqual("企业政策\nPolicy", blocks[0].text)

    def test_two_columns_are_read_left_then_right(self) -> None:
        blocks = [
            OCRTextBlock("左栏第一段", 0.9, 50, 100, 300, 60),
            OCRTextBlock("右栏第一段", 0.9, 600, 110, 300, 60),
            OCRTextBlock("左栏第二段", 0.9, 50, 240, 300, 60),
            OCRTextBlock("右栏第二段", 0.9, 600, 250, 300, 60),
        ]
        ordered, multi_column, reordered = order_ocr_blocks(blocks)
        self.assertTrue(multi_column)
        self.assertTrue(reordered)
        self.assertEqual(
            ["左栏第一段", "左栏第二段", "右栏第一段", "右栏第二段"],
            [block.text for block in ordered],
        )

    def test_low_confidence_fragmented_text_has_lower_quality(self) -> None:
        good_blocks = [OCRTextBlock("退款将在五到十个工作日内到账。", 0.95, 0, 0, 300, 30)]
        bad_blocks = [
            OCRTextBlock("退", 0.42, 0, 0, 10, 10),
            OCRTextBlock("款", 0.38, 20, 20, 10, 10),
            OCRTextBlock("□", 0.2, 40, 40, 10, 10),
        ]
        good = score_ocr_quality(good_blocks[0].text, 0.95, good_blocks)
        bad = score_ocr_quality("退\n款\n□", 0.35, bad_blocks)
        self.assertGreater(good, bad)
        self.assertGreaterEqual(good, 0.8)
        self.assertLess(bad, 0.5)


class OCRRepairSafetyTest(unittest.TestCase):
    def test_business_fields_are_protected(self) -> None:
        text = "合同 SLA-2026 于 2026-07-23 生效，金额为 ¥12,500，联系 ops@example.com。"
        self.assertEqual(
            ["SLA-2026", "2026-07-23", "¥12,500", "ops@example.com"],
            extract_protected_tokens(text),
        )

    def test_large_rewrite_can_be_detected(self) -> None:
        self.assertGreater(length_change_ratio("原始文字", "完全不同且增加了很多内容"), 0.22)


if __name__ == "__main__":
    unittest.main()
