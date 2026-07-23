"""按需修复低质量 OCR 文本的轻量 LLM 客户端。

这个模块只处理已经由 Tesseract 识别出的文本，不负责看图。调用模型前会提取
金额、日期、编号、邮箱和 URL 等受保护字段；模型输出只要改变了这些字段，就会
被拒绝，避免“语句更通顺了，但政策数字被改错”的问题。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DEFAULT_OCR_REPAIR_MODEL = "Qwen/Qwen3-14B"


class OCRRepairError(Exception):
    """OCR 修复请求失败或结果不满足安全约束。"""


@dataclass(frozen=True)
class OCRRepairResult:
    text: str
    changes: tuple[str, ...]
    model: str


def repair_ocr_text(
    text: str,
    *,
    document_name: str,
    page_number: int,
    quality_score: float,
    visual_context: str = "",
) -> OCRRepairResult | None:
    """在配置允许时修复 OCR 断句、错序和明显识别错误。

    返回 ``None`` 表示没有配置 API、功能被关闭或文本不适合发送给模型。
    抛出 ``OCRRepairError`` 表示已经尝试调用，但请求或校验失败。
    """
    enabled = os.getenv("OCR_LLM_REPAIR_ENABLED", "true").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None

    base_url = (
        os.getenv("OCR_REPAIR_BASE_URL")
        or os.getenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
    ).rstrip("/")
    api_key = resolve_api_key(base_url)
    if not api_key:
        return None

    max_chars = max(500, int(os.getenv("OCR_REPAIR_MAX_CHARS", "6000")))
    if not text.strip() or len(text) > max_chars:
        return None

    model = os.getenv("OCR_REPAIR_MODEL", DEFAULT_OCR_REPAIR_MODEL)
    protected_tokens = extract_protected_tokens(text)
    prompt = {
        "task": "修复 OCR 文本的阅读顺序、断行和明显识别错误",
        "hard_constraints": [
            "不得总结、扩写、删减事实或改变原意",
            "必须保留所有金额、数字、日期、编号、邮箱、URL 和产品名",
            "只在上下文非常明确时纠正单个 OCR 错字，不确定的内容原样保留",
            "visual_context 只用于校对 OCR，不得把其中额外的概括内容扩写进原文",
            "恢复标题、段落、列表顺序，但不要输出 Markdown 代码块",
            "如果无法可靠修复，repaired_text 原样返回",
        ],
        "document": document_name,
        "page_number": page_number,
        "ocr_quality_score": round(quality_score, 4),
        "protected_tokens": protected_tokens,
        "visual_context": visual_context or "无；只能根据 OCR 文本本身做保守修复",
        "ocr_text": text,
        "required_json": {
            "repaired_text": "完整修复文本",
            "changes": ["简短说明实际做过的修复；没有修复则为空数组"],
        },
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是企业知识库 OCR 校对器，只负责保守恢复阅读顺序和断句。"
                    "严禁创造原文不存在的信息。只输出合法 JSON。"
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "temperature": 0,
        "max_tokens": int(
            os.getenv(
                "OCR_REPAIR_MAX_TOKENS",
                str(max(320, min(1600, round(len(text) * 1.8)))),
            )
        ),
        # Qwen3 的 OCR 校对任务不需要长链路推理，关闭 thinking 能显著降低延迟。
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }
    timeout = float(os.getenv("OCR_REPAIR_TIMEOUT_SECONDS", "60"))

    last_error: Exception | None = None
    content: str | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
            break
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.8)
    if content is None:
        raise OCRRepairError(f"OCR repair request failed after retry: {last_error}") from last_error

    try:
        parsed = json.loads(strip_json_fence(str(content)))
        repaired_text = str(parsed["repaired_text"]).strip()
        raw_changes = parsed.get("changes") or []
        changes = tuple(str(item).strip() for item in raw_changes if str(item).strip())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise OCRRepairError("OCR repair response was not valid JSON.") from exc

    if not repaired_text:
        raise OCRRepairError("OCR repair returned empty text.")
    if extract_protected_tokens(repaired_text) != protected_tokens:
        raise OCRRepairError("OCR repair changed protected numbers, dates, identifiers, email addresses, or URLs.")
    if length_change_ratio(text, repaired_text) > 0.22:
        raise OCRRepairError("OCR repair changed too much text and was rejected.")
    return OCRRepairResult(text=repaired_text, changes=changes, model=model)


def extract_protected_tokens(text: str) -> list[str]:
    """提取不能被 LLM 改写的业务字段，并保留出现次数与顺序。"""
    pattern = re.compile(
        r"https?://[^\s<>()]+"
        r"|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"
        r"|(?:¥|￥|\$|€|£)\s?\d[\d,]*(?:\.\d+)?"
        r"|\b\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\b"
        r"|\b[A-Z]{1,8}[-_]\d[A-Z0-9_-]*\b"
        r"|\b\d+(?:\.\d+)?%?\b",
        flags=re.IGNORECASE,
    )
    return [match.group(0) for match in pattern.finditer(text)]


def length_change_ratio(original: str, repaired: str) -> float:
    denominator = max(1, len(original))
    return abs(len(repaired) - len(original)) / denominator


def strip_json_fence(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def resolve_api_key(base_url: str) -> str | None:
    explicit = os.getenv("OCR_REPAIR_API_KEY")
    if explicit:
        return explicit
    if "siliconflow" in base_url:
        return os.getenv("SILICONFLOW_API_KEY") or None
    return os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or None
