"""运行客服邮件识别、分类、风险判断和 RAG 召回的离线评测。

默认模式只调用本地规则，不访问 LLM 或 Embedding API，适合快速回归：

    python evaluation/evaluate.py

增加 ``--with-rag`` 后会连接当前知识库并调用项目配置的向量模型：

    python evaluation/evaluate.py --with-rag
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.knowledge import retrieve_knowledge  # noqa: E402
from app.models import EmailRecord  # noqa: E402
from app.workflow import (  # noqa: E402
    build_email_context,
    preprocess_node,
    relevance_gate_node,
    semantic_analysis_node,
    semantic_relevance_gate_node,
)


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    language: str
    customer_name: str
    customer_email: str
    subject: str
    body: str
    expected_support: bool
    expected_category: str
    expected_risk: str
    expected_sources: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass
class CaseResult:
    id: str
    expected_support: bool
    predicted_support: bool
    expected_category: str
    predicted_category: str
    expected_risk: str
    predicted_risk: str
    expected_language: str
    predicted_language: str
    confidence: float
    expected_sources: list[str]
    retrieved_sources: list[str]
    retrieval_latency_ms: int
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate the customer support email agent.")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).with_name("dataset.jsonl"),
        help="JSONL evaluation dataset.",
    )
    parser.add_argument(
        "--with-rag",
        action="store_true",
        help="Evaluate live RAG retrieval. This can call the configured embedding API.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Only evaluate the first N cases.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).with_name("reports"),
        help="Directory for JSON and Markdown reports.",
    )
    return parser.parse_args()


def load_dataset(path: Path, limit: int = 0) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            case_id = str(payload["id"])
            if case_id in seen_ids:
                raise ValueError(f"Duplicate case id {case_id!r} at line {line_number}.")
            seen_ids.add(case_id)
            cases.append(
                EvaluationCase(
                    id=case_id,
                    language=str(payload["language"]),
                    customer_name=str(payload["customer_name"]),
                    customer_email=str(payload["customer_email"]),
                    subject=str(payload["subject"]),
                    body=str(payload["body"]),
                    expected_support=bool(payload["expected_support"]),
                    expected_category=str(payload.get("expected_category", "other")),
                    expected_risk=str(payload.get("expected_risk", "low")),
                    expected_sources=tuple(payload.get("expected_sources") or ()),
                    tags=tuple(payload.get("tags") or ()),
                )
            )
            if limit > 0 and len(cases) >= limit:
                break
    if not cases:
        raise ValueError("Evaluation dataset is empty.")
    return cases


def run_local_pipeline(case: EvaluationCase) -> EmailRecord:
    """执行不访问外部模型的低成本识别与分类链路。"""
    email = EmailRecord(
        customer_name=case.customer_name,
        customer_email=case.customer_email,
        subject=case.subject,
        body=case.body,
    )
    state: dict[str, Any] = {"email": email, "use_llm": False}
    state.update(preprocess_node(state))
    state.update(relevance_gate_node(state))
    if email.status == "irrelevant":
        return email
    state.update(semantic_analysis_node(state))
    state.update(semantic_relevance_gate_node(state))
    return email


def document_source(source: str) -> str:
    """把 ``policy.md#chunk-3`` 归一化为文档级来源 ``policy.md``。"""
    return source.split("#chunk-", maxsplit=1)[0].replace("\\", "/")


def unique_sources(hits: list[Any], limit: int = 3) -> list[str]:
    sources: list[str] = []
    for hit in hits:
        source = document_source(hit.source)
        if source not in sources:
            sources.append(source)
        if len(sources) >= limit:
            break
    return sources


def source_matches(actual: str, expected: set[str]) -> bool:
    normalized_actual = document_source(actual).lower()
    return any(
        normalized_actual == document_source(item).lower()
        or normalized_actual.endswith(f"/{document_source(item).lower()}")
        for item in expected
    )


def evaluate_case(case: EvaluationCase, with_rag: bool) -> CaseResult:
    try:
        email = run_local_pipeline(case)
        predicted_support = email.status != "irrelevant"
        retrieved_sources: list[str] = []
        retrieval_latency_ms = 0
        if with_rag and case.expected_support and case.expected_sources:
            started = time.perf_counter()
            hits = retrieve_knowledge(
                build_email_context(email),
                category=case.expected_category,
                limit=8,
            )
            retrieval_latency_ms = round((time.perf_counter() - started) * 1000)
            retrieved_sources = unique_sources(hits, limit=3)
        return CaseResult(
            id=case.id,
            expected_support=case.expected_support,
            predicted_support=predicted_support,
            expected_category=case.expected_category,
            predicted_category=email.category or "other",
            expected_risk=case.expected_risk,
            predicted_risk=email.risk_level,
            expected_language=case.language,
            predicted_language=email.detected_language,
            confidence=email.confidence,
            expected_sources=list(case.expected_sources),
            retrieved_sources=retrieved_sources,
            retrieval_latency_ms=retrieval_latency_ms,
        )
    except Exception as exc:  # 评测不能因单条脏数据中断整批报告。
        return CaseResult(
            id=case.id,
            expected_support=case.expected_support,
            predicted_support=False,
            expected_category=case.expected_category,
            predicted_category="error",
            expected_risk=case.expected_risk,
            predicted_risk="error",
            expected_language=case.language,
            predicted_language="error",
            confidence=0.0,
            expected_sources=list(case.expected_sources),
            retrieved_sources=[],
            retrieval_latency_ms=0,
            error=str(exc),
        )


def safe_divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def classification_metrics(
    expected: list[str],
    predicted: list[str],
    labels: list[str],
) -> dict[str, Any]:
    per_label: dict[str, dict[str, float | int]] = {}
    f1_scores: list[float] = []
    for label in labels:
        true_positive = sum(1 for exp, pred in zip(expected, predicted) if exp == label and pred == label)
        false_positive = sum(1 for exp, pred in zip(expected, predicted) if exp != label and pred == label)
        false_negative = sum(1 for exp, pred in zip(expected, predicted) if exp == label and pred != label)
        precision = safe_divide(true_positive, true_positive + false_positive)
        recall = safe_divide(true_positive, true_positive + false_negative)
        f1 = safe_divide(2 * precision * recall, precision + recall)
        f1_scores.append(f1)
        per_label[label] = {
            "support": sum(1 for item in expected if item == label),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }
    accuracy = safe_divide(sum(exp == pred for exp, pred in zip(expected, predicted)), len(expected))
    return {
        "accuracy": round(accuracy, 4),
        "macro_f1": round(statistics.mean(f1_scores), 4) if f1_scores else 0.0,
        "per_label": per_label,
    }


def evaluation_split(case_id: str) -> str:
    """按样本 ID 稳定划分校准集和留出集，防止调参时反复窥视留出结果。"""
    bucket = int(hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:8], 16) % 10
    return "calibration" if bucket < 7 else "holdout"


def build_metrics(cases: list[EvaluationCase], results: list[CaseResult], with_rag: bool) -> dict[str, Any]:
    true_positive = sum(item.expected_support and item.predicted_support for item in results)
    false_positive = sum(not item.expected_support and item.predicted_support for item in results)
    false_negative = sum(item.expected_support and not item.predicted_support for item in results)
    true_negative = sum(not item.expected_support and not item.predicted_support for item in results)
    precision = safe_divide(true_positive, true_positive + false_positive)
    recall = safe_divide(true_positive, true_positive + false_negative)
    support_f1 = safe_divide(2 * precision * recall, precision + recall)

    support_results = [
        item for item in results if item.expected_support and item.predicted_support and not item.error
    ]
    category_labels = ["refund", "billing", "technical", "product_question", "complaint"]
    category = classification_metrics(
        [item.expected_category for item in support_results],
        [item.predicted_category for item in support_results],
        category_labels,
    )
    risk = classification_metrics(
        [item.expected_risk for item in support_results],
        [item.predicted_risk for item in support_results],
        ["low", "medium", "high"],
    )
    risk["splits"] = {}
    for split_name in ("calibration", "holdout"):
        split_results = [
            item for item in support_results if evaluation_split(item.id) == split_name
        ]
        split_metrics = classification_metrics(
            [item.expected_risk for item in split_results],
            [item.predicted_risk for item in split_results],
            ["low", "medium", "high"],
        )
        split_metrics["cases"] = len(split_results)
        risk["splits"][split_name] = split_metrics
    language_accuracy = safe_divide(
        sum(item.expected_language == item.predicted_language for item in results if not item.error),
        sum(not item.error for item in results),
    )

    metrics: dict[str, Any] = {
        "dataset": {
            "cases": len(cases),
            "support_cases": sum(case.expected_support for case in cases),
            "non_support_cases": sum(not case.expected_support for case in cases),
            "languages": dict(Counter(case.language for case in cases)),
            "tags": dict(Counter(tag for case in cases for tag in case.tags)),
        },
        "support_detection": {
            "accuracy": round(safe_divide(true_positive + true_negative, len(results)), 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(support_f1, 4),
            "confusion_matrix": {
                "true_positive": true_positive,
                "false_positive": false_positive,
                "false_negative": false_negative,
                "true_negative": true_negative,
            },
        },
        "category": category,
        "risk": risk,
        "language_accuracy": round(language_accuracy, 4),
        "errors": sum(bool(item.error) for item in results),
    }

    if with_rag:
        rag_results = [
            item
            for item in results
            if item.expected_support and item.expected_sources and not item.error
        ]
        reciprocal_ranks: list[float] = []
        recall_at_1: list[float] = []
        recall_at_3: list[float] = []
        precision_at_3: list[float] = []
        for item in rag_results:
            expected_sources = {source.lower() for source in item.expected_sources}
            ranks = [
                index
                for index, source in enumerate(item.retrieved_sources, start=1)
                if source_matches(source, expected_sources)
            ]
            reciprocal_ranks.append(1 / ranks[0] if ranks else 0.0)
            recall_at_1.append(1.0 if ranks and ranks[0] == 1 else 0.0)
            recall_at_3.append(1.0 if ranks else 0.0)
            relevant_count = sum(
                source_matches(source, expected_sources) for source in item.retrieved_sources[:3]
            )
            precision_at_3.append(relevant_count / 3)
        latencies = [item.retrieval_latency_ms for item in rag_results]
        metrics["rag"] = {
            "evaluated_cases": len(rag_results),
            "recall_at_1": round(statistics.mean(recall_at_1), 4) if recall_at_1 else 0.0,
            "recall_at_3": round(statistics.mean(recall_at_3), 4) if recall_at_3 else 0.0,
            "mrr": round(statistics.mean(reciprocal_ranks), 4) if reciprocal_ranks else 0.0,
            "precision_at_3": round(statistics.mean(precision_at_3), 4) if precision_at_3 else 0.0,
            "average_latency_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
            "p95_latency_ms": percentile(latencies, 0.95),
        }
    return metrics


def percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return ordered[index]


def build_failures(results: list[CaseResult], with_rag: bool) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for item in results:
        reasons: list[str] = []
        if item.expected_support != item.predicted_support:
            reasons.append("support_detection")
        if item.expected_support and item.predicted_support:
            if item.expected_category != item.predicted_category:
                reasons.append("category")
            if item.expected_risk != item.predicted_risk:
                reasons.append("risk")
        if item.expected_language != item.predicted_language:
            reasons.append("language")
        if with_rag and item.expected_support and item.expected_sources:
            expected = {source.lower() for source in item.expected_sources}
            if not any(source_matches(source, expected) for source in item.retrieved_sources[:3]):
                reasons.append("rag_recall_at_3")
        if item.error:
            reasons.append("runtime_error")
        if reasons:
            payload = asdict(item)
            payload["failed_metrics"] = reasons
            failures.append(payload)
    return failures


def markdown_report(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    support = metrics["support_detection"]
    category = metrics["category"]
    risk = metrics["risk"]
    lines = [
        "# 客服邮件 Agent 基线评测报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 数据集：{report['dataset_path']}",
        f"- 样本数：{metrics['dataset']['cases']}",
        f"- 模式：{'本地规则 + 在线 RAG' if report['with_rag'] else '本地规则（零外部模型调用）'}",
        "",
        "## 核心指标",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        f"| 客服识别 Accuracy | {support['accuracy']:.2%} |",
        f"| 客服识别 Precision | {support['precision']:.2%} |",
        f"| 客服识别 Recall | {support['recall']:.2%} |",
        f"| 客服识别 F1 | {support['f1']:.2%} |",
        f"| 分类 Accuracy | {category['accuracy']:.2%} |",
        f"| 分类 Macro-F1 | {category['macro_f1']:.2%} |",
        f"| 风险 Accuracy | {risk['accuracy']:.2%} |",
        f"| 风险 Macro-F1 | {risk['macro_f1']:.2%} |",
        f"| 风险 Macro-F1（校准集） | {risk['splits']['calibration']['macro_f1']:.2%} |",
        f"| 风险 Macro-F1（留出集） | {risk['splits']['holdout']['macro_f1']:.2%} |",
        f"| 语言识别 Accuracy | {metrics['language_accuracy']:.2%} |",
    ]
    if report["with_rag"]:
        rag = metrics["rag"]
        lines.extend(
            [
                f"| RAG Recall@1 | {rag['recall_at_1']:.2%} |",
                f"| RAG Recall@3 | {rag['recall_at_3']:.2%} |",
                f"| RAG MRR | {rag['mrr']:.4f} |",
                f"| RAG Precision@3 | {rag['precision_at_3']:.2%} |",
                f"| RAG 平均耗时 | {rag['average_latency_ms']:.1f} ms |",
                f"| RAG P95 耗时 | {rag['p95_latency_ms']} ms |",
            ]
        )
    lines.extend(
        [
            "",
            "## 失败样本",
            "",
            f"共 {len(report['failures'])} 条样本至少有一项未达到标注结果。",
            "",
            "| ID | 失败环节 | 预期 | 实际 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for failure in report["failures"]:
        expected = (
            f"support={failure['expected_support']}, "
            f"category={failure['expected_category']}, risk={failure['expected_risk']}"
        )
        actual = (
            f"support={failure['predicted_support']}, "
            f"category={failure['predicted_category']}, risk={failure['predicted_risk']}"
        )
        lines.append(
            f"| {failure['id']} | {', '.join(failure['failed_metrics'])} | {expected} | {actual} |"
        )
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- 分类和风险指标只统计被正确放行到客服链路的客服邮件，避免把过滤错误重复计算。",
            "- 风险校准集与留出集按样本 ID 的 SHA-256 稳定划分；规则调优只参考校准集，留出集用于最终验证。",
            "- RAG 使用标注类别进行检索，用于单独评估检索器，不把上游分类错误混入召回指标。",
            "- Precision@3 按三个结果槽位计算；即使只标注一个正确来源，其理论上限也可能低于 100%。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    cases = load_dataset(args.dataset, args.limit)
    results = [evaluate_case(case, args.with_rag) for case in cases]
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset_path": str(args.dataset.resolve()),
        "with_rag": args.with_rag,
        "metrics": build_metrics(cases, results, args.with_rag),
        "failures": build_failures(results, args.with_rag),
        "results": [asdict(item) for item in results],
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    suffix = "rag" if args.with_rag else "rules"
    json_path = args.output_dir / f"baseline-{suffix}.json"
    markdown_path = args.output_dir / f"baseline-{suffix}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")

    print(markdown_report(report))
    print(f"\nJSON report: {json_path.resolve()}")
    print(f"Markdown report: {markdown_path.resolve()}")
    return 0 if report["metrics"]["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
