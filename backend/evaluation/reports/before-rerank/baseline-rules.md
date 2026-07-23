# 客服邮件 Agent 基线评测报告

- 生成时间：2026-07-23T08:57:32+00:00
- 数据集：/app/backend/evaluation/dataset.jsonl
- 样本数：200
- 模式：本地规则（零外部模型调用）

## 核心指标

| 指标 | 结果 |
| --- | ---: |
| 客服识别 Accuracy | 93.00% |
| 客服识别 Precision | 97.01% |
| 客服识别 Recall | 92.86% |
| 客服识别 F1 | 94.89% |
| 分类 Accuracy | 61.54% |
| 分类 Macro-F1 | 67.82% |
| 风险 Accuracy | 52.31% |
| 风险 Macro-F1 | 52.30% |
| 语言识别 Accuracy | 100.00% |

## 失败样本

共 85 条样本至少有一项未达到标注结果。

| ID | 失败环节 | 预期 | 实际 |
| --- | --- | --- | --- |
| refund-zh-01 | risk | support=True, category=refund, risk=high | support=True, category=refund, risk=low |
| refund-zh-02 | category | support=True, category=refund, risk=high | support=True, category=product_question, risk=high |
| refund-zh-04 | support_detection | support=True, category=refund, risk=high | support=False, category=other, risk=low |
| billing-zh-02 | category, risk | support=True, category=billing, risk=medium | support=True, category=refund, risk=low |
| billing-zh-03 | category, risk | support=True, category=billing, risk=medium | support=True, category=refund, risk=low |
| billing-en-01 | risk | support=True, category=billing, risk=medium | support=True, category=billing, risk=high |
| billing-en-03 | category, risk | support=True, category=billing, risk=medium | support=True, category=product_question, risk=low |
| technical-zh-04 | category, risk | support=True, category=technical, risk=medium | support=True, category=other, risk=low |
| product-en-04 | category, risk | support=True, category=product_question, risk=low | support=True, category=technical, risk=medium |
| complaint-zh-03 | risk | support=True, category=complaint, risk=high | support=True, category=complaint, risk=low |
| complaint-en-03 | risk | support=True, category=complaint, risk=high | support=True, category=complaint, risk=low |
| nonsupport-zh-04 | support_detection | support=False, category=other, risk=low | support=True, category=product_question, risk=low |
| nonsupport-zh-07 | support_detection | support=False, category=other, risk=low | support=True, category=product_question, risk=low |
| nonsupport-en-07 | support_detection | support=False, category=other, risk=low | support=True, category=other, risk=low |
| ext-refund-zh-01 | risk | support=True, category=refund, risk=high | support=True, category=refund, risk=low |
| ext-refund-zh-02 | risk | support=True, category=refund, risk=medium | support=True, category=refund, risk=high |
| ext-refund-en-02 | risk | support=True, category=refund, risk=medium | support=True, category=refund, risk=high |
| ext-refund-zh-03 | risk | support=True, category=refund, risk=medium | support=True, category=refund, risk=low |
| ext-refund-en-03 | risk | support=True, category=refund, risk=medium | support=True, category=refund, risk=low |
| ext-refund-zh-05 | category, risk | support=True, category=refund, risk=high | support=True, category=billing, risk=medium |
| ext-refund-en-05 | risk | support=True, category=refund, risk=high | support=True, category=refund, risk=low |
| ext-refund-zh-07 | risk | support=True, category=refund, risk=medium | support=True, category=refund, risk=high |
| ext-refund-en-07 | risk | support=True, category=refund, risk=medium | support=True, category=refund, risk=high |
| ext-refund-zh-08 | risk | support=True, category=refund, risk=medium | support=True, category=refund, risk=high |
| ext-refund-en-08 | risk | support=True, category=refund, risk=medium | support=True, category=refund, risk=high |
| ext-refund-zh-10 | risk | support=True, category=refund, risk=medium | support=True, category=refund, risk=high |
| ext-refund-en-10 | risk | support=True, category=refund, risk=medium | support=True, category=refund, risk=high |
| ext-billing-en-01 | category, risk | support=True, category=billing, risk=medium | support=True, category=refund, risk=low |
| ext-billing-en-02 | category, risk | support=True, category=billing, risk=medium | support=True, category=complaint, risk=high |
| ext-billing-zh-03 | category, risk | support=True, category=billing, risk=medium | support=True, category=refund, risk=low |
| ext-billing-zh-04 | category, risk | support=True, category=billing, risk=medium | support=True, category=refund, risk=low |
| ext-billing-en-04 | category, risk | support=True, category=billing, risk=medium | support=True, category=refund, risk=low |
| ext-billing-zh-05 | category, risk | support=True, category=billing, risk=medium | support=True, category=other, risk=low |
| ext-billing-en-05 | support_detection | support=True, category=billing, risk=medium | support=False, category=other, risk=low |
| ext-billing-zh-07 | category, risk | support=True, category=billing, risk=medium | support=True, category=refund, risk=low |
| ext-billing-en-07 | category, risk | support=True, category=billing, risk=medium | support=True, category=refund, risk=low |
| ext-billing-zh-09 | category, risk | support=True, category=billing, risk=medium | support=True, category=other, risk=low |
| ext-billing-en-09 | category, risk | support=True, category=billing, risk=medium | support=True, category=other, risk=low |
| ext-billing-zh-10 | category, risk | support=True, category=billing, risk=medium | support=True, category=refund, risk=low |
| ext-technical-zh-02 | support_detection | support=True, category=technical, risk=medium | support=False, category=other, risk=low |
| ext-technical-en-02 | support_detection | support=True, category=technical, risk=medium | support=False, category=other, risk=low |
| ext-technical-en-03 | risk | support=True, category=technical, risk=high | support=True, category=technical, risk=medium |
| ext-technical-zh-04 | category, risk | support=True, category=technical, risk=high | support=True, category=other, risk=low |
| ext-technical-en-04 | support_detection | support=True, category=technical, risk=high | support=False, category=other, risk=low |
| ext-technical-zh-05 | risk | support=True, category=technical, risk=high | support=True, category=technical, risk=medium |
| ext-technical-en-05 | risk | support=True, category=technical, risk=high | support=True, category=technical, risk=medium |
| ext-technical-zh-06 | category, risk | support=True, category=technical, risk=high | support=True, category=product_question, risk=low |
| ext-technical-en-06 | category, risk | support=True, category=technical, risk=high | support=True, category=product_question, risk=low |
| ext-technical-zh-07 | category, risk | support=True, category=technical, risk=high | support=True, category=other, risk=low |
| ext-technical-en-07 | category, risk | support=True, category=technical, risk=high | support=True, category=other, risk=low |
| ext-technical-zh-09 | support_detection | support=True, category=technical, risk=high | support=False, category=other, risk=low |
| ext-technical-en-09 | category, risk | support=True, category=technical, risk=high | support=True, category=other, risk=low |
| ext-product_question-zh-02 | category | support=True, category=product_question, risk=low | support=True, category=other, risk=low |
| ext-product_question-en-02 | category, risk | support=True, category=product_question, risk=low | support=True, category=technical, risk=medium |
| ext-product_question-zh-03 | category, risk | support=True, category=product_question, risk=low | support=True, category=technical, risk=medium |
| ext-product_question-en-03 | support_detection | support=True, category=product_question, risk=low | support=False, category=other, risk=low |
| ext-product_question-en-04 | category | support=True, category=product_question, risk=low | support=True, category=other, risk=low |
| ext-product_question-zh-06 | category | support=True, category=product_question, risk=low | support=True, category=other, risk=low |
| ext-product_question-en-06 | category | support=True, category=product_question, risk=low | support=True, category=other, risk=low |
| ext-product_question-zh-07 | category | support=True, category=product_question, risk=low | support=True, category=other, risk=low |
| ext-product_question-en-07 | category | support=True, category=product_question, risk=low | support=True, category=other, risk=low |
| ext-product_question-zh-08 | category, risk | support=True, category=product_question, risk=medium | support=True, category=other, risk=low |
| ext-product_question-en-08 | category, risk | support=True, category=product_question, risk=medium | support=True, category=other, risk=low |
| ext-product_question-zh-09 | category, risk | support=True, category=product_question, risk=medium | support=True, category=other, risk=low |
| ext-product_question-en-09 | risk | support=True, category=product_question, risk=medium | support=True, category=product_question, risk=low |
| ext-complaint-zh-01 | category | support=True, category=complaint, risk=high | support=True, category=other, risk=high |
| ext-complaint-en-01 | support_detection | support=True, category=complaint, risk=high | support=False, category=other, risk=low |
| ext-complaint-zh-02 | category, risk | support=True, category=complaint, risk=high | support=True, category=technical, risk=medium |
| ext-complaint-zh-03 | category, risk | support=True, category=complaint, risk=high | support=True, category=refund, risk=low |
| ext-complaint-en-03 | category, risk | support=True, category=complaint, risk=high | support=True, category=refund, risk=low |
| ext-complaint-zh-04 | category | support=True, category=complaint, risk=high | support=True, category=other, risk=high |
| ext-complaint-en-04 | support_detection | support=True, category=complaint, risk=high | support=False, category=other, risk=low |
| ext-complaint-zh-05 | category, risk | support=True, category=complaint, risk=high | support=True, category=other, risk=low |
| ext-complaint-en-05 | category, risk | support=True, category=complaint, risk=high | support=True, category=other, risk=low |
| ext-complaint-zh-06 | risk | support=True, category=complaint, risk=high | support=True, category=complaint, risk=low |
| ext-complaint-en-06 | support_detection | support=True, category=complaint, risk=high | support=False, category=other, risk=low |
| ext-complaint-zh-07 | category, risk | support=True, category=complaint, risk=high | support=True, category=other, risk=low |
| ext-complaint-en-07 | category, risk | support=True, category=complaint, risk=high | support=True, category=other, risk=low |
| ext-complaint-zh-08 | category, risk | support=True, category=complaint, risk=high | support=True, category=other, risk=low |
| ext-complaint-en-08 | category, risk | support=True, category=complaint, risk=high | support=True, category=other, risk=low |
| ext-complaint-zh-09 | category, risk | support=True, category=complaint, risk=high | support=True, category=other, risk=low |
| ext-complaint-en-09 | category, risk | support=True, category=complaint, risk=high | support=True, category=other, risk=low |
| ext-complaint-zh-10 | category, risk | support=True, category=complaint, risk=high | support=True, category=other, risk=low |
| ext-complaint-en-10 | category, risk | support=True, category=complaint, risk=high | support=True, category=other, risk=low |
| ext-nonsupport-zh-17 | support_detection | support=False, category=other, risk=low | support=True, category=billing, risk=medium |

## 说明

- 分类和风险指标只统计被正确放行到客服链路的客服邮件，避免把过滤错误重复计算。
- RAG 使用标注类别进行检索，用于单独评估检索器，不把上游分类错误混入召回指标。
- Precision@3 按三个结果槽位计算；即使只标注一个正确来源，其理论上限也可能低于 100%。
