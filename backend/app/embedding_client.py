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
LOCAL_EMBEDDING_MODEL = "local-hash-embedding-v1"


class EmbeddingClientError(Exception):
    pass


def embed_texts(texts: list[str]) -> tuple[list[list[float]], str]:
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
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def tokenize(text: str) -> list[str]:
    text = text.lower()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text)
    bigrams = [text[index : index + 2] for index in range(max(0, len(text) - 1)) if contains_chinese(text[index : index + 2])]
    return words + bigrams


def contains_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)
