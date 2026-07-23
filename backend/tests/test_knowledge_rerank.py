"""RAG 轻量重排的词项清洗与分类一致性测试。"""

from __future__ import annotations

import unittest

from app.knowledge import (
    category_alignment_score,
    expand_cross_language_terms,
    extract_terms,
    keyword_overlap,
)


class KnowledgeRerankTest(unittest.TestCase):
    def test_english_stop_words_do_not_inflate_keyword_score(self) -> None:
        terms = extract_terms(
            "The latest password reset link is expired and the browser cache was cleared."
        )
        self.assertNotIn("the", terms)
        self.assertNotIn("and", terms)
        self.assertIn("password", terms)
        self.assertIn("password reset", terms)

        relevant = keyword_overlap(
            terms,
            "Use the newest password reset link after clearing browser cache.",
        )
        unrelated = keyword_overlap(
            terms,
            "The customer can review the service level and support priority table.",
        )
        self.assertGreater(relevant, unrelated)

    def test_chinese_subintent_terms_favor_service_incident_content(self) -> None:
        terms = extract_terms("生产 API 持续超时，多个工作区受到服务故障影响")
        incident_score = keyword_overlap(
            terms,
            "生产环境接口超时或核心功能故障时，应查询状态页并升级值班工程师。",
        )
        login_score = keyword_overlap(
            terms,
            "用户无法登录时应重新发送密码重置链接并检查验证码。",
        )
        self.assertGreater(incident_score, login_score)

    def test_category_alignment_distinguishes_subintents_in_same_category(self) -> None:
        terms = extract_terms("OAuth authorization failed with sensitive permissions")
        oauth_score = category_alignment_score(
            "technical",
            terms,
            "technical",
            "OAuth third-party authorization and sensitive permission troubleshooting",
        )
        generic_score = category_alignment_score(
            "technical",
            terms,
            "technical",
            "General service priority and response time table",
        )
        wrong_category_score = category_alignment_score(
            "technical",
            terms,
            "billing",
            "OAuth payment receipt",
        )
        self.assertGreater(oauth_score, generic_score)
        self.assertEqual(0.0, wrong_category_score)

    def test_cross_language_expansion_adds_chinese_business_terms(self) -> None:
        terms = expand_cross_language_terms(
            extract_terms("Production API outage requires supervisor escalation")
        )
        self.assertIn("故障", terms)
        self.assertIn("生产", terms)
        self.assertIn("主管", terms)
        self.assertIn("升级", terms)


if __name__ == "__main__":
    unittest.main()
