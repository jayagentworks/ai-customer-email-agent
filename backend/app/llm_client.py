import json
import os
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class SemanticAnalysisResult(BaseModel):
    category: Literal["refund", "complaint", "technical", "billing", "product_question", "other"]
    confidence: float = Field(ge=0, le=1)
    risk_level: Literal["low", "medium", "high"]
    risk_flags: list[str] = Field(default_factory=list)
    should_escalate: bool
    reason: str


class LLMClientError(Exception):
    pass


def analyze_email_with_llm(
    *,
    subject: str,
    body: str,
    detected_language: str,
    preprocessing_flags: list[str],
) -> SemanticAnalysisResult | None:
    base_url = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "qwen-plus")
    api_key = resolve_llm_api_key(base_url)
    if not api_key:
        return None
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a customer support email semantic analysis agent. "
                    "Return only valid JSON. Do not include markdown. "
                    "Categories must be one of: refund, complaint, technical, billing, product_question, other. "
                    "Risk level must be one of: low, medium, high. "
                    "Use the input language for risk_flags and reason."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "subject": subject,
                        "body": body,
                        "detected_language": detected_language,
                        "preprocessing_flags": preprocessing_flags,
                        "required_json_schema": {
                            "category": "refund|complaint|technical|billing|product_question|other",
                            "confidence": "number between 0 and 1",
                            "risk_level": "low|medium|high",
                            "risk_flags": ["short risk reason strings"],
                            "should_escalate": "boolean",
                            "reason": "short explanation",
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMClientError(f"LLM request failed: {exc}") from exc

    try:
        content = response.json()["choices"][0]["message"]["content"]
        return SemanticAnalysisResult.model_validate_json(content)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise LLMClientError("LLM response was not valid semantic analysis JSON") from exc


def resolve_llm_api_key(base_url: str) -> str | None:
    explicit_key = os.getenv("LLM_API_KEY")
    if explicit_key:
        return explicit_key
    if "siliconflow" in base_url:
        return os.getenv("SILICONFLOW_API_KEY") or None
    return os.getenv("DASHSCOPE_API_KEY") or None
