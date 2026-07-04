import base64
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DEFAULT_VISION_MODEL = "Qwen/Qwen3-VL-8B-Instruct"


class VisionClientError(Exception):
    pass


def is_vision_configured() -> bool:
    base_url = os.getenv("VISION_BASE_URL") or os.getenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
    return bool(resolve_vision_api_key(base_url))


def describe_image(image_path: Path, *, context: str = "") -> str | None:
    base_url = (os.getenv("VISION_BASE_URL") or os.getenv("LLM_BASE_URL", "https://api.siliconflow.cn/v1")).rstrip("/")
    model = os.getenv("VISION_MODEL", DEFAULT_VISION_MODEL)
    api_key = resolve_vision_api_key(base_url)
    if not api_key:
        return None

    max_bytes = int(os.getenv("VISION_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))
    if image_path.stat().st_size > max_bytes:
        return None

    timeout = float(os.getenv("VISION_TIMEOUT_SECONDS", os.getenv("LLM_TIMEOUT_SECONDS", "30")))
    image_data_url = build_image_data_url(image_path)
    prompt = (
        "请为客服知识库中的图片生成一段可用于 RAG 检索的中文说明。"
        "重点提取图片中的文字、表格含义、流程关系、产品/界面/截图中的关键信息。"
        "如果图片只是装饰图或没有业务信息，请明确说明它可能无业务语义。"
        "不要编造图片中没有出现的信息。输出 3 到 6 句纯文本，不要使用 Markdown。"
    )
    if context:
        prompt += f"\n图片上下文：{context}"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url, "detail": "auto"}},
                ],
            }
        ],
        "temperature": 0.1,
        "max_tokens": int(os.getenv("VISION_MAX_TOKENS", "320")),
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise VisionClientError(f"Vision request failed: {exc}") from exc

    description = " ".join(str(content).split()).strip()
    return description or None


def build_image_data_url(image_path: Path) -> str:
    mime_type = guess_image_mime_type(image_path.suffix)
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def guess_image_mime_type(suffix: str) -> str:
    suffix = suffix.lower().lstrip(".")
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
        "bmp": "image/bmp",
    }.get(suffix, "image/png")


def resolve_vision_api_key(base_url: str) -> str | None:
    explicit_key = os.getenv("VISION_API_KEY")
    if explicit_key:
        return explicit_key
    if "siliconflow" in base_url:
        return os.getenv("SILICONFLOW_API_KEY") or None
    return os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or None
