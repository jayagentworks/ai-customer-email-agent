"""大语言模型客户端。

本模块封装所有 chat/completions 调用：
- ``analyze_email_with_llm``：用于语义分析，要求模型返回严格 JSON。
- ``generate_reply_draft_with_llm``：用于生成面向客户的回复草稿。

这样业务流程层不用关心具体模型平台，只要通过环境变量切换 base_url、
model 和 api key 即可。
"""

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
    """LLM 语义分析结果的强约束结构。

    使用 Pydantic 校验可以防止模型返回格式漂移，例如 category 拼错、
    confidence 超出 0-1 范围，或者 risk_level 不在枚举范围内。
    """

    category: Literal["refund", "complaint", "technical", "billing", "product_question", "other"]
    is_support_request: bool
    confidence: float = Field(ge=0, le=1)
    risk_level: Literal["low", "medium", "high"]
    risk_flags: list[str] = Field(default_factory=list)
    should_escalate: bool
    reason: str


class LLMClientError(Exception):
    pass


def is_llm_configured() -> bool:
    """检查当前环境是否配置了可用 LLM API Key。"""
    base_url = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    return bool(resolve_llm_api_key(base_url))


def analyze_email_with_llm(
    *,
    subject: str,
    body: str,
    detected_language: str,
    preprocessing_flags: list[str],
) -> SemanticAnalysisResult | None:
    """调用 LLM 做邮件语义分析。

    该调用只负责“判断”，不生成回复。temperature 设置较低，是为了让分类和风险判断
    尽量稳定；同时要求 JSON 输出，方便工作流节点可靠解析。
    """
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
                    "First decide whether the sender is asking OUR customer support team to answer a question "
                    "or solve a concrete customer need. The is_support_request boolean is the authoritative "
                    "routing decision and must never be inferred merely from category, confidence, or risk. "
                    "Set is_support_request to true only when the sender/customer is requesting an answer, "
                    "investigation, correction, refund, technical assistance, or another concrete support action. "
                    "Set it to false when the sender is offering help, introducing a product, welcoming or "
                    "onboarding the recipient, sending marketing/newsletters, platform notifications, security "
                    "notices, verification codes, surveys, recommendations, or other messages that do not ask "
                    "OUR support team to solve a customer problem. Words such as help, support, issue, or problem "
                    "alone are insufficient; identify who is asking whom for help. "
                    "Return only valid JSON matching the requested schema. Do not include markdown or extra text. "
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
                            "is_support_request": "boolean; true only when the sender asks support to answer or solve a customer need",
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


def generate_reply_draft_with_llm(
    *,
    customer_name: str,
    subject: str,
    body: str,
    category: str | None,
    risk_level: str,
    detected_language: str,
    knowledge_hits: list[dict],
    variant: str = "default",
) -> str | None:
    """调用 LLM 生成客服回复草稿。

    输入中只传入 Top-K 知识片段和必要邮件上下文，避免把过多无关知识塞进 prompt。
    系统提示词明确禁止输出 markdown、引用行、chunk id 和内部流程信息，
    保证草稿更像真实客服回复。
    """
    base_url = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model = os.getenv("LLM_MODEL", "qwen-plus")
    api_key = resolve_llm_api_key(base_url)
    if not api_key:
        return None
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    max_tokens = int(os.getenv("LLM_DRAFT_MAX_TOKENS", "420"))
    compact_hits = [
        {
            **hit,
            "snippet": compact_text(str(hit.get("snippet", "")), 700),
        }
        for hit in knowledge_hits[:2]
    ]

    style_instruction = {
        "default": "Use a clear, professional, concise support tone.",
        "alternative-1": "Use a warmer support tone while staying specific.",
        "alternative-2": "Use a more direct and action-oriented support tone.",
        "alternative-3": "Use a slightly more detailed support tone without adding unsupported promises.",
    }.get(variant, "Use a clear, professional, concise support tone.")

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a customer support reply drafting agent for a RAG email workflow. "
                    "Write only the customer-facing reply body. Do not include markdown, JSON, citations, "
                    "internal labels, confidence scores, or reference lines. "
                    "Answer the customer's explicit question directly before asking for more information. "
                    "Use only facts supported by the provided knowledge snippets. If the snippets do not support "
                    "a claim, say that it needs confirmation instead of inventing details. "
                    "For refund, contract, legal, security, account access, or high-risk cases, do not promise an outcome; "
                    "state the review or escalation path. "
                    "Match the detected language: use Chinese for zh and English for en."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "customer_name": customer_name,
                        "subject": subject,
                        "body": compact_text(body, 1200),
                        "category": category,
                        "risk_level": risk_level,
                        "detected_language": detected_language,
                        "reply_variant": variant,
                        "style_instruction": style_instruction,
                        "knowledge_hits": compact_hits,
                        "requirements": [
                            "Start with a natural greeting.",
                            "Directly answer the main customer question using the knowledge hits.",
                            "Explain the next step only when needed.",
                            "Ask for missing information only after giving the best supported answer.",
                            "End with a support-team signoff.",
                            "Do not mention 'Reference used', chunk ids, RAG, knowledge base, or internal workflow.",
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "temperature": 0.35 if variant == "default" else 0.55,
        "max_tokens": max_tokens,
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
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMClientError("LLM response did not contain a reply draft") from exc

    draft = content.strip()
    if not draft:
        raise LLMClientError("LLM reply draft was empty")
    return draft


def compact_text(text: str, max_chars: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars].rstrip()}..."


def resolve_llm_api_key(base_url: str) -> str | None:
    explicit_key = os.getenv("LLM_API_KEY")
    if explicit_key:
        return explicit_key
    if "siliconflow" in base_url:
        return os.getenv("SILICONFLOW_API_KEY") or None
    return os.getenv("DASHSCOPE_API_KEY") or None
