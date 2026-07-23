"""邮件风险评分与 LLM 风险护栏测试。"""

from __future__ import annotations

import unittest

from app.risk import RiskAssessment, assess_email_risk, merge_llm_risk


class RiskAssessmentTest(unittest.TestCase):
    def test_product_feature_question_is_low_risk(self) -> None:
        result = assess_email_risk(
            "Does the Pro plan support audit logs and team workspaces?",
            "product_question",
            [],
            "en",
        )
        self.assertEqual("low", result.level)

    def test_routine_refund_status_is_medium_risk(self) -> None:
        result = assess_email_risk(
            "请问退款申请目前处理到哪一步，预计多久到账？",
            "refund",
            [],
            "zh",
        )
        self.assertEqual("medium", result.level)

    def test_duplicate_charge_is_high_risk(self) -> None:
        result = assess_email_risk(
            "My subscription was charged twice. Please reverse the duplicate charge.",
            "refund",
            [],
            "en",
        )
        self.assertEqual("high", result.level)

    def test_production_outage_is_high_risk(self) -> None:
        result = assess_email_risk(
            "Production is unavailable and our entire team cannot work.",
            "technical",
            ["business_blocked"],
            "en",
        )
        self.assertEqual("high", result.level)

    def test_routine_password_reset_is_medium_risk(self) -> None:
        result = assess_email_risk(
            "重置密码链接已过期，请重新发送一个链接。",
            "technical",
            [],
            "zh",
        )
        self.assertEqual("medium", result.level)

    def test_issued_invoice_does_not_match_sue(self) -> None:
        result = assess_email_risk(
            "The invoice was issued with the wrong company name. Can it be corrected?",
            "billing",
            [],
            "en",
        )
        self.assertEqual("medium", result.level)

    def test_product_permission_question_stays_low_risk(self) -> None:
        result = assess_email_risk(
            "Can Pro assign different access levels to administrators and read-only users?",
            "technical",
            [],
            "en",
        )
        self.assertEqual("low", result.level)

    def test_unauthorized_automatic_renewal_is_high_risk(self) -> None:
        result = assess_email_risk(
            "We did not approve this automatic renewal, but the subscription was charged.",
            "refund",
            [],
            "en",
        )
        self.assertEqual("high", result.level)

    def test_widespread_core_job_failure_is_high_risk(self) -> None:
        result = assess_email_risk(
            "Sixty percent of core synchronization jobs failed with E503.",
            "other",
            [],
            "en",
        )
        self.assertEqual("high", result.level)

    def test_request_for_duplicate_charge_evidence_is_medium_risk(self) -> None:
        result = assess_email_risk(
            "为了核对重复扣费，我应该提供订单号还是支付截图？",
            "refund",
            [],
            "zh",
        )
        self.assertEqual("medium", result.level)

    def test_escalated_account_closure_complaint_is_high_risk(self) -> None:
        result = assess_email_risk(
            "Repeated outages remain unresolved. Escalate this and close our enterprise account.",
            "other",
            [],
            "en",
        )
        self.assertEqual("high", result.level)

    def test_llm_cannot_downgrade_rule_guardrail(self) -> None:
        final_level, flags = merge_llm_risk(
            "low",
            ["模型认为影响有限"],
            RiskAssessment("high", 4, ("存在数据泄露风险",)),
        )
        self.assertEqual("high", final_level)
        self.assertEqual(["模型认为影响有限", "存在数据泄露风险"], flags)

    def test_llm_can_raise_rule_result(self) -> None:
        final_level, _ = merge_llm_risk(
            "high",
            ["模型识别到隐含法律威胁"],
            RiskAssessment("medium", 2, ("常规账单处理",)),
        )
        self.assertEqual("high", final_level)


if __name__ == "__main__":
    unittest.main()
