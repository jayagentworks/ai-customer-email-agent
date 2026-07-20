import unittest

from app.llm_client import SemanticAnalysisResult
from app.models import EmailRecord
from app.workflow import (
    apply_llm_analysis,
    is_customer_support_request,
    semantic_relevance_gate_node,
)


def make_email(subject: str, body: str, *, sender_name: str = "Customer", sender_email: str = "customer@example.com") -> EmailRecord:
    return EmailRecord(
        customer_name=sender_name,
        customer_email=sender_email,
        subject=subject,
        body=body,
    )


class RelevanceGateTests(unittest.TestCase):
    def test_welcome_email_offering_help_is_not_a_support_request(self) -> None:
        email = make_email(
            "Welcome to RentCast! Let's help you get started",
            "Explore the platform and complete your profile to get started. Need help? Read our guide. Unsubscribe.",
            sender_name="Anton from RentCast",
            sender_email="anton@rentcast.io",
        )

        relevant, reason = is_customer_support_request(email)

        self.assertFalse(relevant)
        self.assertIn("welcome", reason.lower())

    def test_welcome_word_does_not_hide_a_real_customer_request(self) -> None:
        email = make_email(
            "Welcome email received, but I cannot log in",
            "Please help me access my workspace. My login keeps failing.",
        )

        relevant, _ = is_customer_support_request(email)

        self.assertTrue(relevant)

    def test_plain_support_word_is_not_sufficient_without_customer_intent(self) -> None:
        email = make_email(
            "Our support team is here to help",
            "Read our product tour and explore the latest features.",
            sender_name="Product Team",
            sender_email="updates@vendor.example",
        )

        relevant, _ = is_customer_support_request(email)

        self.assertFalse(relevant)

    def test_structured_llm_non_support_decision_blocks_the_workflow(self) -> None:
        email = make_email(
            "A message with wording not covered by local rules",
            "This message does not ask the service team to resolve anything.",
        )
        result = SemanticAnalysisResult(
            category="other",
            is_support_request=False,
            confidence=0.99,
            risk_level="low",
            risk_flags=[],
            should_escalate=False,
            reason="This is a welcome email, not a support request.",
        )
        apply_llm_analysis(email, result)

        output = semantic_relevance_gate_node(
            {"email": email, "use_llm": True, "semantic_is_support_request": result.is_support_request}
        )

        self.assertEqual(output["email"].status, "irrelevant")
        self.assertEqual(output["email"].category, "other")
        self.assertEqual(output["email"].draft_reply, "")
        self.assertEqual(output["email"].steps[-1].status, "blocked")


if __name__ == "__main__":
    unittest.main()
