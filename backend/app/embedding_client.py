"""Embedding 客户端与本地降级向量实现。

正常情况下系统会调用外部 embedding 模型生成语义向量；如果没有配置 API Key
或外部请求失败，会退回到本地 hash embedding。后者语义能力较弱，但可以保证
演示和测试环境不会因为模型服务不可用而完全中断。
"""

import hashlib
import math
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

DEFAULT_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
# 本地 hash embedding 只作为降级方案，不建议在真实生产检索质量评估中使用。
LOCAL_EMBEDDING_MODEL = "local-hash-embedding-v1"


class EmbeddingClientError(Exception):
    pass


def embed_texts(texts: list[str]) -> tuple[list[list[float]], str]:
    """批量生成文本向量。

    返回值包含向量列表和实际使用的模型名，方便写入数据库后追踪 embedding 来源。
    """
    base_url = os.getenv("EMBEDDING_BASE_URL") or os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    base_url = base_url.rstrip("/")
    model = os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    api_key = resolve_embedding_api_key(base_url)
    if not api_key:
        return [local_embedding(text) for text in texts], LOCAL_EMBEDDING_MODEL
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{base_url}/embeddings",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "input": texts},
            )
            response.raise_for_status()
            payload = response.json()
            vectors = [item["embedding"] for item in sorted(payload["data"], key=lambda item: item["index"])]
            return vectors, model
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        vectors = [local_embedding(text) for text in texts]
        return vectors, LOCAL_EMBEDDING_MODEL


def resolve_embedding_api_key(base_url: str) -> str | None:
    explicit_key = os.getenv("EMBEDDING_API_KEY")
    if explicit_key:
        return explicit_key
    if "siliconflow" in base_url:
        return os.getenv("SILICONFLOW_API_KEY") or None
    return os.getenv("DASHSCOPE_API_KEY") or None


def embed_text(text: str) -> tuple[list[float], str]:
    vectors, model = embed_texts([text])
    return vectors[0], model


def local_embedding(text: str, dimensions: int = 256) -> list[float]:
    """本地 hash embedding 降级实现。

    它把 token hash 到固定维度向量中，再做 L2 归一化。这个方法不具备真实语义理解，
    但能保留一定关键词相似性，适合模型平台不可用时兜底。
    """
    vector = [0.0] * dimensions
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [round(value / norm, 6) for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个向量的余弦相似度。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def tokenize(text: str) -> list[str]:
    """将中英文文本切成用于本地 hash embedding 的 token。

    英文按单词切，中文按单字和 bigram 补充，尽量兼顾中文短词匹配。
    """
    text = text.lower()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text)
    bigrams = [text[index : index + 2] for index in range(max(0, len(text) - 1)) if contains_chinese(text[index : index + 2])]
    return words + bigrams


def contains_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)
