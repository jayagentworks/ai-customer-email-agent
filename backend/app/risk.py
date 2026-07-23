"""可解释的邮件风险评分。

风险与邮件分类是两个不同问题：分类回答“客户在问什么”，风险回答“如果处理错误，
业务后果有多严重”。本模块不依赖 LLM，也不把某个分类直接等同于高风险，而是综合
安全、法律、业务影响、财务争议、重复联系和常规处理信号进行评分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    score: int
    flags: tuple[str, ...]


RISK_ORDER: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2}


def assess_email_risk(
    text: str,
    category: str | None,
    preprocessing_flags: list[str] | None = None,
    language: str = "zh",
) -> RiskAssessment:
    """根据邮件事实计算风险等级，不依赖分类置信度或知识库置信度。"""
    normalized = text.lower()
    score = 0
    flags: list[str] = []

    # “能否支持某功能”和“功能已经故障”不是同一种风险。即使上游分类偶尔把
    # 产品咨询分成 technical，这些问句也不应仅因出现 access/login/compliance
    # 等能力名称就自动升为中风险。
    is_product_capability_question = contains_any(
        normalized,
        {
            "does pro",
            "can pro",
            "does the pro",
            "what actions are included",
            "roles and permissions",
            "different access levels",
            "是否支持",
            "是否包含",
            "可以保留哪些",
            "记录哪些操作",
            "包含登录",
            "成员权限",
        },
    ) and not contains_any(
        normalized,
        {
            "failed",
            "unavailable",
            "not working",
            "cannot access",
            "locked",
            "失效",
            "失败",
            "不可用",
            "无法访问",
            "被锁",
        },
    )

    # 询问“核验需要什么材料”属于常规流程咨询，不能只因为提到重复扣费就升级。
    is_financial_evidence_question = contains_any(
        normalized,
        {
            "what should i provide",
            "which documents should",
            "what evidence",
            "需要哪些资料",
            "应该提供",
            "要提供哪些",
            "为了核对",
        },
    )

    def ensure_score(
        minimum: int,
        english_flag: str,
        chinese_flag: str,
    ) -> None:
        nonlocal score
        score = max(score, minimum)
        flag = chinese_flag if language == "zh" else english_flag
        if flag not in flags:
            flags.append(flag)

    # 常规退款、账单、技术问题通常需要人工谨慎处理，但本身不等于高风险。
    if category in {"refund", "billing", "technical"} and not is_product_capability_question:
        ensure_score(
            2,
            "Routine financial or technical issue requires review.",
            "常规财务或技术问题需要谨慎处理。",
        )
    if category == "complaint":
        ensure_score(
            2,
            "Customer complaint requires careful review.",
            "客户投诉需要谨慎审核。",
        )

    if contains_any(
        normalized,
        {
            "refund",
            "charged",
            "duplicate charge",
            "payment",
            "退款",
            "退费",
            "扣费",
            "重复收费",
            "支付",
        },
    ):
        ensure_score(2, "Financial action or refund is involved.", "邮件涉及退款或资金处理。")

    if contains_any(
        normalized,
        {
            "invoice",
            "billing",
            "receipt",
            "tax id",
            "purchase order",
            "procurement",
            "发票",
            "账单",
            "收据",
            "税号",
            "采购订单",
            "企业采购",
        },
    ):
        ensure_score(2, "Billing or procurement handling is required.", "邮件涉及账单、发票或采购处理。")

    if not is_product_capability_question and contains_any(
        normalized,
        {
            "login",
            "password",
            "verification code",
            "oauth",
            "mfa",
            "access",
            "登录",
            "密码",
            "验证码",
            "授权",
            "多因素认证",
            "无法访问",
        },
    ):
        ensure_score(2, "Account access or technical troubleshooting is required.", "邮件涉及账号访问或技术排查。")

    # 企业采购、价格、合同和合规咨询需要转交专业团队，但没有争议时属于中风险。
    if not is_product_capability_question and contains_any(
        normalized,
        {
            "pricing",
            "quotation",
            "discount",
            "contract terms",
            "compliance",
            "dpa",
            "报价",
            "折扣",
            "合同条款",
            "合规",
            "安全白皮书",
        },
    ):
        ensure_score(2, "Commercial or compliance commitment needs specialist review.", "商业或合规承诺需要专业团队审核。")

    # 明确的资金争议比普通退款进度查询风险更高。
    has_charge_signal = contains_any(
        normalized,
        {"charge", "charged", "payment", "扣费", "扣款", "支付"},
    )
    has_duplicate_signal = contains_any(
        normalized,
        {
            "duplicate charge",
            "duplicate payment",
            "charged twice",
            "two identical",
            "twice this month",
            "重复扣费",
            "重复收费",
            "重复支付",
            "扣了两次",
            "两笔相同",
        },
    )
    has_unauthorized_renewal = (
        contains_any(normalized, {"unauthorized", "not approve", "未授权", "没有批准"})
        and contains_any(normalized, {"renewal", "charged", "续费", "扣款", "扣费"})
    )
    has_charged_after_cancel = (
        contains_any(normalized, {"cancelled", "canceled", "cancel subscription", "取消", "已取消"})
        and contains_any(normalized, {"still charged", "charged automatically", "仍被扣", "仍然续费", "仍被扣款"})
    )
    has_material_refund_request = contains_any(
        normalized,
        {
            "large refund",
            "refund dispute",
            "cross-currency",
            "mistaken annual purchase",
            "trial converted to a paid",
            "converted to a paid subscription",
            "大额退款",
            "退款争议",
            "跨币种",
            "误购买",
            "误操作购买",
        },
    )
    if not is_financial_evidence_question and (
        (has_charge_signal and has_duplicate_signal)
        or has_unauthorized_renewal
        or has_charged_after_cancel
        or has_material_refund_request
    ):
        ensure_score(4, "A material payment dispute requires escalation.", "存在实质性资金争议，需要升级处理。")

    # 安全、隐私和法律信号直接进入高风险，不受分类结果影响。
    if contains_any(
        normalized,
        {
            "account stolen",
            "account compromised",
            "unknown device",
            "sensitive permission",
            "data leak",
            "data breach",
            "personal data",
            "privacy officer",
            "delete data",
            "lawyer",
            "legal action",
            "sue",
            "账号被盗",
            "未知设备",
            "敏感权限",
            "数据泄露",
            "个人数据",
            "隐私负责人",
            "删除数据",
            "律师",
            "法律行动",
            "起诉",
        },
    ):
        ensure_score(4, "Security, privacy, or legal exposure requires escalation.", "存在安全、隐私或法律风险，需要升级处理。")

    # 生产环境中断、多人受影响或核心流程不可用属于高业务影响。
    has_production_context = contains_any(
        normalized,
        {
            "production",
            "core workflow",
            "core page",
            "core synchronization",
            "core task",
            "multiple workspaces",
            "multiple team members",
            "entire team",
            "生产",
            "核心流程",
            "核心页面",
            "核心任务",
            "同步任务",
            "多个工作区",
            "多个团队成员",
            "整个团队",
            "停工",
        },
    )
    has_outage_context = contains_any(
        normalized,
        {
            "outage",
            "incident",
            "unavailable",
            "timed out",
            "timeout",
            "blocked",
            "cannot work",
            "failed",
            "e503",
            "故障",
            "中断",
            "不可用",
            "超时",
            "受阻",
            "无法使用",
            "大量失败",
            "六成",
        },
    )
    has_widespread_impact = contains_any(
        normalized,
        {
            "sixty percent",
            "most core",
            "forty people",
            "two departments",
            "impact is growing",
            "六成",
            "大量执行失败",
            "40 人",
            "两个部门",
            "错误仍在扩大",
        },
    )
    if has_outage_context and (has_production_context or has_widespread_impact):
        ensure_score(4, "Production or multi-user business operations are blocked.", "生产环境或多人业务流程受阻。")

    # 账号锁定、认证循环、敏感 OAuth 等会阻断访问，应高于普通登录咨询。
    if contains_any(
        normalized,
        {
            "account locked",
            "locked account",
            "mfa loop",
            "mfa is looping",
            "mfa, the page returns",
            "authentication loop",
            "multiple reset",
            "sensitive oauth",
            "账号被锁",
            "账号锁定",
            "认证循环",
            "多因素认证循环",
            "多次重置",
            "敏感 oauth",
        },
    ):
        ensure_score(4, "Account security or authentication failure blocks access.", "账号安全或认证故障阻断访问。")

    # 明确投诉、强烈不满、重复催促、要求主管或取消账号时直接升级。
    if contains_any(
        normalized,
        {
            "formal complaint",
            "very dissatisfied",
            "very unhappy",
            "supervisor",
            "manager take over",
            "followed up three",
            "third follow",
            "lawyer will contact",
            "close our account",
            "close our enterprise account",
            "escalate this",
            "generic processing message",
            "tell me the owner",
            "next update time",
            "missed its sla",
            "no response for",
            "ignored the evidence",
            "投诉",
            "非常不满",
            "主管",
            "接手",
            "催促三次",
            "跟进三次",
            "律师联系",
            "关闭企业账号",
            "模板回复",
            "请升级",
            "要求明确负责人",
            "告诉我负责人",
            "下一次更新时间",
            "超过 sla",
            "违反高优先级 sla",
            "没有答复",
            "没有解决",
        },
    ):
        ensure_score(4, "Escalated customer dissatisfaction requires supervisor review.", "客户不满或重复催促已经升级，需要主管审核。")

    # 预处理只作为补充证据，附件本身不会提高风险。
    preprocessing_flags = preprocessing_flags or []
    if "legal_risk" in preprocessing_flags:
        ensure_score(4, "Preprocessing detected legal language.", "预处理检测到法律风险。")
    if "repeated_contact" in preprocessing_flags:
        ensure_score(4, "Preprocessing detected repeated customer contact.", "预处理检测到客户重复联系。")
    if "business_blocked" in preprocessing_flags and has_production_context:
        ensure_score(4, "Preprocessing confirmed material business impact.", "预处理确认存在明显业务影响。")

    if is_product_capability_question and score < 4:
        score = 0
        flags = [
            "产品能力咨询未包含实际故障或争议信号。"
            if language == "zh"
            else "Product capability question has no active failure or dispute signal."
        ]

    level: RiskLevel = "high" if score >= 4 else ("medium" if score >= 2 else "low")
    return RiskAssessment(level=level, score=score, flags=tuple(flags))


def merge_llm_risk(
    llm_level: RiskLevel,
    llm_flags: list[str],
    rules: RiskAssessment,
) -> tuple[RiskLevel, list[str]]:
    """合并 LLM 判断与确定性护栏。

    规则只提供风险下限：明确安全、法律、生产中断等信号不能被模型降级；
    模型仍可基于更完整语义把规则未识别的邮件提高风险。
    """
    final_level = (
        rules.level
        if RISK_ORDER[rules.level] > RISK_ORDER[llm_level]
        else llm_level
    )
    merged_flags = list(dict.fromkeys([*llm_flags, *rules.flags]))
    return final_level, merged_flags


def contains_any(text: str, terms: set[str]) -> bool:
    """匹配中文子串或完整英文词组，避免 ``sue`` 命中 ``issued``。"""
    for term in terms:
        normalized_term = term.lower()
        if re.search(r"[a-z0-9]", normalized_term):
            pattern = rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])"
            if re.search(pattern, text):
                return True
        elif normalized_term in text:
            return True
    return False
