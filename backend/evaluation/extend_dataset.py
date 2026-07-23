"""将首批 60 条评测样本扩展为 200 条可复现的双语评测集。

扩展样本不是简单替换人名或同义词，而是覆盖同一业务分类下的不同子意图：
退款进度、预授权、税号修改、采购材料、OAuth、服务事故、SLA、数据请求等。
中英文样本使用相同业务事实，便于观察跨语言检索表现是否一致。
"""

from __future__ import annotations

import json
from pathlib import Path


DATASET_PATH = Path(__file__).with_name("dataset.jsonl")
BASE_CASE_COUNT = 60
TARGET_CASE_COUNT = 200


SUPPORT_SCENARIOS = {
    "refund": [
        (
            "duplicate-charge",
            "high",
            ["refund_policy.md"],
            "同一账期出现两笔相同扣费",
            "本月同一个工作区出现两笔 299 元订阅扣费，请核对支付流水并协助退回重复的一笔。",
            "Two identical subscription charges in one billing cycle",
            "Our workspace was charged USD 39 twice this month. Please verify both transactions and refund the duplicate.",
        ),
        (
            "refund-arrival",
            "medium",
            ["refund_policy.md"],
            "退款申请通过后多久能到账",
            "退款申请已经创建，请问款项通常需要几个工作日原路退回？",
            "When will the approved refund arrive",
            "The refund request has been created. How many business days does it normally take to return to the original payment method?",
        ),
        (
            "bank-authorization",
            "medium",
            ["refund_policy.md"],
            "银行卡里有一笔待处理扣款",
            "账单中心只有一笔正式扣费，但银行卡还有一笔相同金额处于待处理状态，这是不是预授权？",
            "A second card charge is still pending",
            "The billing center shows one settled payment, but my card has another identical pending amount. Could it be a temporary authorization hold?",
        ),
        (
            "charged-after-cancel",
            "high",
            ["refund_policy.md", "test_samples/scanned_cancellation_policy_ocr.pdf"],
            "取消订阅后仍然续费",
            "我在续费日前取消了订阅，但今天仍被扣款，请核验取消时间并处理退款。",
            "Charged after cancelling before renewal",
            "I cancelled before the renewal date but was charged today. Please verify the cancellation timestamp and review the refund.",
        ),
        (
            "unauthorized-renewal",
            "high",
            ["refund_policy.md"],
            "未授权的自动续费",
            "我们没有批准这次续费，账单却自动扣款了。请调查订阅状态，不要直接关闭工单。",
            "Unauthorized automatic renewal",
            "We did not approve this renewal, but the subscription was charged automatically. Please investigate the subscription status.",
        ),
        (
            "cross-currency-refund",
            "high",
            ["refund_policy.md", "sla_escalation_policy.md"],
            "跨币种企业合同退款",
            "企业合同使用美元线下付款，现在需要退回部分款项并换算成人民币，请转账单主管处理。",
            "Cross-currency enterprise contract refund",
            "Our enterprise contract was paid offline in USD and now requires a partial refund converted to CNY. Please escalate it to billing.",
        ),
        (
            "refund-proof",
            "medium",
            ["refund_policy.md"],
            "退款核验需要哪些资料",
            "为了核对重复扣费，我应该提供订单号、支付时间还是支付渠道截图？",
            "Information required to verify a refund",
            "For a duplicate-charge review, should I provide the order ID, payment time, amount, or a payment-channel screenshot?",
        ),
        (
            "cancel-current-cycle",
            "medium",
            ["refund_policy.md", "test_samples/scanned_cancellation_policy_ocr.pdf"],
            "续费后才取消能否退款",
            "扣款完成两天后我才取消订阅，这个账期还能申请退款吗？",
            "Cancellation happened after renewal",
            "I cancelled two days after the renewal charge. Can the current billing cycle still be reviewed for a refund?",
        ),
        (
            "large-refund-dispute",
            "high",
            ["refund_policy.md", "sla_escalation_policy.md"],
            "大额退款争议需要主管介入",
            "这笔企业账单金额很大，我们已经催促三次仍没有结果，请主管立即介入。",
            "Large refund dispute needs supervisor review",
            "This enterprise charge is substantial and we have followed up three times without a resolution. A supervisor needs to review it.",
        ),
        (
            "refund-status",
            "medium",
            ["refund_policy.md"],
            "查询退款处理状态",
            "工单显示正在退款，但银行卡还没有到账，请帮我确认目前处于哪一步。",
            "Check the current refund status",
            "The ticket says the refund is processing, but nothing has reached my card. Please confirm the current stage.",
        ),
    ],
    "billing": [
        (
            "invoice-delay",
            "medium",
            ["billing_invoice_policy.md"],
            "付款后还没有生成发票",
            "我们三天前完成月付，账单中心仍没有发票记录，请确认正常生成时间。",
            "Invoice has not appeared after payment",
            "We completed the monthly payment three days ago, but no invoice record is available. What is the normal generation time?",
        ),
        (
            "invoice-title-change",
            "medium",
            ["billing_invoice_policy.md"],
            "已开发票需要修改抬头",
            "发票已经开具，但公司名称填写错误，是否需要红冲或作废后重新开票？",
            "Change the title on an issued invoice",
            "The invoice has already been issued with the wrong company name. Does it require cancellation or a credit-note process?",
        ),
        (
            "tax-id",
            "medium",
            ["billing_invoice_policy.md"],
            "补录企业税号",
            "付款时漏填了税号，现在能否补录并重新生成正确的发票？",
            "Add a missing company tax ID",
            "We omitted the tax ID during payment. Can it be added before a corrected invoice is generated?",
        ),
        (
            "annual-contract-invoice",
            "medium",
            ["billing_invoice_policy.md"],
            "年付合同什么时候开票",
            "企业年付合同已经签署，请问是付款后开票还是需要销售先确认合同信息？",
            "Invoice timing for an annual contract",
            "Our annual enterprise contract is signed. Is the invoice issued after payment or after sales verifies the contract details?",
        ),
        (
            "purchase-order",
            "medium",
            ["billing_invoice_policy.md", "test_samples/enterprise_purchase_checklist.txt"],
            "采购订单和供应商准入",
            "采购部门需要报价单、PO 和供应商准入材料，请告诉我应提交哪些公司信息。",
            "Purchase order and vendor onboarding",
            "Procurement needs a quotation, purchase order support, and vendor-onboarding documents. What company information should we provide?",
        ),
        (
            "receipt",
            "medium",
            ["billing_invoice_policy.md"],
            "需要付款收据用于报销",
            "这笔信用卡付款能否提供包含账单编号和付款日期的正式收据？",
            "Payment receipt required for expenses",
            "Can you provide an official receipt containing the billing number and payment date for this card payment?",
        ),
        (
            "contract-payment",
            "medium",
            ["billing_invoice_policy.md", "test_samples/enterprise_purchase_checklist.txt"],
            "合同付款后如何开通",
            "公司准备通过合同转账付款，财务确认到账后多久可以开通服务？",
            "Activation after contract payment",
            "We plan to pay by contract bank transfer. When can service be activated after finance confirms receipt?",
        ),
        (
            "invoice-address",
            "medium",
            ["billing_invoice_policy.md"],
            "修改发票注册地址和开户行",
            "账单资料中的注册地址和开户行已经变更，下一张发票应如何更新？",
            "Update invoice address and bank details",
            "Our registered address and bank details have changed. How should we update them for the next invoice?",
        ),
        (
            "annual-discount",
            "medium",
            ["test_samples/enterprise_purchase_checklist.txt", "billing_invoice_policy.md"],
            "咨询年付采购折扣",
            "我们预计采购 120 个席位并签年付合同，希望销售提供正式报价和折扣方案。",
            "Annual procurement discount request",
            "We expect to purchase 120 seats on an annual contract and need sales to provide a formal quotation and discount proposal.",
        ),
        (
            "billing-reconciliation",
            "medium",
            ["billing_invoice_policy.md"],
            "账单金额与采购订单不一致",
            "本月账单金额和采购订单不一致，请协助核对账单编号、席位数和付款记录。",
            "Invoice amount does not match the purchase order",
            "This month's invoice amount differs from our purchase order. Please reconcile the billing ID, seat count, and payment record.",
        ),
    ],
    "technical": [
        (
            "expired-reset-link",
            "medium",
            ["login_troubleshooting.md"],
            "密码重置链接已失效",
            "我点击最新的重置密码邮件仍提示链接过期，清理缓存后也没有解决。",
            "The latest password reset link is expired",
            "The newest password-reset email still says the link is expired, even after I cleared the browser cache.",
        ),
        (
            "verification-code",
            "medium",
            ["login_troubleshooting.md"],
            "企业邮箱收不到验证码",
            "登录验证码没有进入收件箱或垃圾箱，可能被公司邮件网关拦截了。",
            "Verification code is missing from corporate email",
            "The login code is not in the inbox or spam folder and may have been blocked by our corporate mail gateway.",
        ),
        (
            "locked-account",
            "high",
            ["login_troubleshooting.md", "sla_escalation_policy.md"],
            "多次输错密码后账号被锁",
            "管理员账号被临时锁定，生产工作区现在无法访问，请安全团队协助。",
            "Account locked after failed password attempts",
            "The administrator account is locked and our production workspace is inaccessible. We need account-security assistance.",
        ),
        (
            "oauth-error",
            "high",
            ["login_troubleshooting.md"],
            "第三方 OAuth 授权异常",
            "连接报表应用时提示敏感权限授权失败，我们不会扩大权限，请帮助检查错误码。",
            "Third-party OAuth authorization error",
            "Connecting our reporting app fails on a sensitive OAuth scope. We will not broaden permissions and need help checking the error.",
        ),
        (
            "unknown-login",
            "high",
            ["login_troubleshooting.md", "sla_escalation_policy.md"],
            "发现未知设备登录",
            "审计日志出现陌生设备登录记录，我们怀疑账号被盗，请立即升级安全处理。",
            "Unknown device appeared in the login log",
            "The audit log shows an unfamiliar device. We suspect account compromise and need immediate security escalation.",
        ),
        (
            "api-timeout",
            "high",
            ["test_samples/service_incident_response.md", "sla_escalation_policy.md"],
            "生产 API 持续超时",
            "从 10:20 开始所有订单接口都超时，影响三个工作区，请确认是否存在服务故障。",
            "Production API requests keep timing out",
            "All order API requests have timed out since 10:20 and three workspaces are affected. Please check for an active incident.",
        ),
        (
            "partial-outage",
            "high",
            ["test_samples/service_incident_response.md"],
            "核心任务大量执行失败",
            "过去半小时有六成同步任务失败，错误码 E503，状态页暂时没有公告。",
            "Most core synchronization jobs are failing",
            "Sixty percent of sync jobs failed in the last 30 minutes with E503, while the status page has no notice.",
        ),
        (
            "sso-member-access",
            "medium",
            ["login_troubleshooting.md"],
            "部分成员无法通过 SSO 登录",
            "管理员可以登录，但新加入的成员使用 SSO 后一直返回工作区无权限。",
            "Some members cannot sign in through SSO",
            "Administrators can sign in, but newly added members are denied workspace access after SSO authentication.",
        ),
        (
            "mfa-loop",
            "high",
            ["login_troubleshooting.md"],
            "多因素认证陷入循环",
            "完成 MFA 后页面又回到验证码界面，已更换浏览器仍然重复。",
            "Multi-factor authentication is looping",
            "After completing MFA, the page returns to the verification screen. The loop continues in another browser.",
        ),
        (
            "workspace-access",
            "medium",
            ["login_troubleshooting.md"],
            "成员突然失去工作区访问权限",
            "一名成员昨天还能使用工作区，今天登录后所有项目都不可见，请协助排查权限。",
            "A member suddenly lost workspace access",
            "A member could use the workspace yesterday, but all projects disappeared after signing in today. Please investigate access.",
        ),
    ],
    "product_question": [
        (
            "team-workspace",
            "low",
            ["product_faq.md"],
            "Pro 是否支持多人团队工作区",
            "我们有 25 名成员，需要共享项目和任务，Pro 套餐是否支持在同一工作区协作？",
            "Does Pro support a shared team workspace",
            "We have 25 members who need to share projects and tasks. Does Pro support collaboration in one workspace?",
        ),
        (
            "role-permission",
            "low",
            ["product_faq.md"],
            "Pro 的成员角色和权限",
            "能否给管理员、普通成员、只读成员和外部协作者配置不同访问权限？",
            "Member roles and permissions in Pro",
            "Can Pro assign different access levels to administrators, members, read-only users, and external collaborators?",
        ),
        (
            "audit-log",
            "low",
            ["product_faq.md"],
            "审计日志记录哪些操作",
            "Pro 审计日志是否包含登录、权限变更、配置修改和数据导出？",
            "Which events are included in the audit log",
            "Does the Pro audit log include sign-ins, permission changes, configuration updates, and data exports?",
        ),
        (
            "webhook-api",
            "low",
            ["product_faq.md"],
            "Pro 是否提供 Webhook 和 API",
            "我们希望把业务事件同步到内部系统，Pro 套餐支持 Webhook、API 和数据同步吗？",
            "Webhook and API support in Pro",
            "We need to send business events to an internal system. Does Pro provide webhooks, APIs, and data synchronization?",
        ),
        (
            "sso-integration",
            "low",
            ["product_faq.md"],
            "Pro 能否接入企业 SSO",
            "团队使用统一身份平台，想确认 Pro 是否支持 SSO 和第三方身份集成。",
            "Enterprise SSO integration in Pro",
            "Our team uses a central identity provider. Does Pro support SSO and third-party identity integration?",
        ),
        (
            "priority-support",
            "low",
            ["product_faq.md"],
            "Pro 的优先支持有什么区别",
            "升级后问题是否会进入优先队列？这种能力更适合哪些团队？",
            "How Pro priority support works",
            "Will requests enter a priority queue after upgrading, and what type of team benefits from it?",
        ),
        (
            "plan-fit",
            "low",
            ["product_faq.md"],
            "成长型团队是否适合 Pro",
            "我们正在快速扩张，需要统一权限和追踪关键操作，Pro 是否适合这种使用场景？",
            "Is Pro suitable for a growing team",
            "We are growing quickly and need centralized permissions and traceable operations. Is Pro suited to this scenario?",
        ),
        (
            "compliance-promise",
            "medium",
            ["product_faq.md", "test_samples/enterprise_purchase_checklist.txt"],
            "Pro 能否保证满足行业合规",
            "采购希望确认 Pro 一定符合我们行业的合规要求，并需要安全白皮书和 DPA。",
            "Can Pro guarantee industry compliance",
            "Procurement wants a guarantee that Pro satisfies our industry compliance rules and needs a security white paper and DPA.",
        ),
        (
            "pricing-contract",
            "medium",
            ["test_samples/enterprise_purchase_checklist.txt", "product_faq.md"],
            "Pro 报价和合同咨询",
            "计划购买 80 个 Pro 席位，请提供价格、合同条款和采购周期。",
            "Pro pricing and contract inquiry",
            "We plan to buy 80 Pro seats and need pricing, contract terms, and an estimated procurement timeline.",
        ),
        (
            "feature-comparison",
            "low",
            ["product_faq.md"],
            "基础套餐与 Pro 功能差异",
            "主要关注团队协作、权限、审计和高级集成，请按这四项说明 Pro 的优势。",
            "Feature differences between Basic and Pro",
            "We care about collaboration, permissions, auditing, and advanced integrations. Please explain Pro's advantages in these areas.",
        ),
    ],
    "complaint": [
        (
            "slow-response",
            "high",
            ["test_samples/complaint_escalation_policy.docx", "sla_escalation_policy.md"],
            "多次催促仍未收到处理结果",
            "这个问题已经跟进三次，客服每次都只说正在处理，我要求主管给出明确进展。",
            "Repeated follow-ups without a resolution",
            "We have followed up three times and only receive processing updates. I need a supervisor to provide concrete progress.",
        ),
        (
            "production-blocked",
            "high",
            ["test_samples/service_incident_response.md", "sla_escalation_policy.md"],
            "故障导致生产业务完全中断",
            "核心页面从早上起无法访问，整个运营团队停工，请按最高优先级升级。",
            "Incident has completely blocked production",
            "The core page has been unavailable since this morning and our operations team cannot work. Escalate at the highest priority.",
        ),
        (
            "refund-complaint",
            "high",
            ["refund_policy.md", "sla_escalation_policy.md"],
            "重复扣费投诉一直没有处理",
            "重复扣费工单已经超过两天没有答复，如果今天还不处理我要正式投诉。",
            "Complaint about an unresolved duplicate charge",
            "Our duplicate-charge ticket has had no response for two days. If it is not handled today, I will file a formal complaint.",
        ),
        (
            "legal-threat",
            "high",
            ["test_samples/complaint_escalation_policy.docx", "sla_escalation_policy.md"],
            "要求法务和主管介入",
            "你们未经同意修改了服务条款，如果无法解释，我会让律师联系你们。",
            "Legal and supervisor review requested",
            "You changed the service terms without consent. If this cannot be explained, our lawyer will contact you.",
        ),
        (
            "cancel-account",
            "high",
            ["test_samples/complaint_escalation_policy.docx", "test_samples/scanned_cancellation_policy_ocr.pdf"],
            "服务体验太差要求关闭账号",
            "连续出现故障且客服没有解决，我不再接受模板回复，请升级并协助关闭企业账号。",
            "Poor service and request to close the account",
            "Repeated outages remain unresolved. I will not accept another template reply; escalate this and help close our enterprise account.",
        ),
        (
            "data-concern",
            "high",
            ["test_samples/privacy_data_request_text_layer.pdf", "sla_escalation_policy.md"],
            "投诉数据导出和隐私处理",
            "我要求知道哪些个人数据被导出，并删除不应保留的数据，请交给隐私负责人。",
            "Complaint about exported personal data",
            "I need to know what personal data was exported and request deletion of data that should not be retained. Route this to privacy.",
        ),
        (
            "missed-sla",
            "high",
            ["sla_escalation_policy.md"],
            "高优先级工单已经超过 SLA",
            "生产问题超过两小时仍无人响应，已经违反高优先级 SLA，请主管接手。",
            "A high-priority ticket has missed its SLA",
            "This production issue has had no response for more than two hours, missing the high-priority SLA. A supervisor must take over.",
        ),
        (
            "multiple-members",
            "high",
            ["test_samples/service_incident_response.md", "sla_escalation_policy.md"],
            "故障影响多个团队成员",
            "两个部门共 40 人无法提交任务，错误仍在扩大，请立即通知值班工程师。",
            "An incident affects multiple team members",
            "Forty people across two departments cannot submit tasks and the impact is growing. Notify the on-call engineer immediately.",
        ),
        (
            "angry-customer",
            "high",
            ["test_samples/complaint_escalation_policy.docx"],
            "对客服处理方式非常不满",
            "客服没有阅读我提供的证据，反复让我执行相同步骤，我对这种处理方式非常不满。",
            "Very dissatisfied with support handling",
            "Support ignored the evidence I provided and repeatedly asked me to perform the same steps. I am very dissatisfied.",
        ),
        (
            "status-demand",
            "high",
            ["test_samples/complaint_escalation_policy.docx", "sla_escalation_policy.md"],
            "要求明确负责人和下一次更新时间",
            "不要再回复正在处理中，请告诉我负责人、已经完成的排查和下一次更新时间。",
            "Demand for an owner and next update time",
            "Do not send another generic processing message. Tell me the owner, completed investigation steps, and next update time.",
        ),
    ],
}


NON_SUPPORT_SCENARIOS = [
    ("github-code", "GitHub Sudo verification code", "Your GitHub verification code is 482911. It expires in 15 minutes.", "GitHub 验证码", "你的 GitHub 验证码是 482911，15 分钟后失效。"),
    ("oauth-added", "A new OAuth application was authorized", "A third-party OAuth application was added to your account. Review it in the security log.", "新的 OAuth 应用已授权", "第三方 OAuth 应用已添加到你的账号，请前往安全日志查看。"),
    ("password-notice", "Your password was changed", "This is an automatic security notice confirming that your password was changed.", "密码已修改通知", "这是一封自动安全通知，用于确认你的密码已经修改。"),
    ("login-alert", "New sign-in from Windows", "We noticed a new sign-in from a Windows device in Singapore.", "检测到新的登录", "系统检测到一台位于新加坡的 Windows 设备登录。"),
    ("newsletter", "July product newsletter", "Read this month's product news, customer stories, and upcoming events.", "七月产品资讯", "查看本月产品动态、客户故事和即将举行的活动。"),
    ("promotion", "Limited-time 30% upgrade offer", "Upgrade before Friday to receive a promotional discount.", "限时升级优惠", "本周五前升级即可获得限时折扣。"),
    ("webinar", "You're invited to our AI webinar", "Reserve your seat for next week's online product webinar.", "邀请参加 AI 线上研讨会", "欢迎预约参加下周的产品线上研讨会。"),
    ("survey", "Help us improve by taking a survey", "Complete a 20-minute survey and receive a gift card.", "参与问卷帮助我们改进", "完成二十分钟问卷可获得礼品卡。"),
    ("terms", "Updated Terms of Service", "Our Terms of Service will change next month. No action is required.", "服务条款更新通知", "服务条款将在下个月更新，你无需执行任何操作。"),
    ("privacy", "Annual privacy policy notice", "This message summarizes our annual privacy policy update.", "年度隐私政策通知", "本邮件用于说明年度隐私政策更新。"),
    ("status-resolved", "Resolved: API latency incident", "The API latency incident has been resolved. This is an automated status notification.", "已解决：API 延迟事件", "API 延迟事件已经解决，这是一封自动状态通知。"),
    ("maintenance", "Scheduled maintenance this weekend", "Planned maintenance starts Saturday at 02:00 UTC.", "本周末计划维护", "计划维护将在周六 02:00 UTC 开始。"),
    ("deprecation", "Legacy API version retirement", "API version v1 will retire on September 30. Review the migration guide.", "旧版 API 即将下线", "API v1 将于 9 月 30 日下线，请查看迁移指南。"),
    ("release", "Version 5.2 is now available", "The latest desktop release includes performance and security improvements.", "5.2 版本现已发布", "最新桌面版本包含性能和安全改进。"),
    ("welcome", "Welcome to your new workspace", "Your workspace is ready. Follow the getting-started guide to invite teammates.", "欢迎使用新工作区", "你的工作区已经准备好，可根据入门指南邀请成员。"),
    ("receipt", "Payment receipt #A-202607", "This automatic receipt confirms your successful subscription payment.", "付款收据 A-202607", "这是一封自动收据，用于确认订阅付款成功。"),
    ("invoice-generated", "Your invoice is ready", "Invoice INV-8821 is available for download in the billing center.", "发票已经生成", "发票 INV-8821 已生成，可前往账单中心下载。"),
    ("delivery", "Your package has shipped", "Your order is on the way. Track shipment with the carrier.", "包裹已发货", "你的订单已经发出，可通过承运商查询物流。"),
    ("job-alert", "New jobs matching your profile", "Five new software engineering positions match your saved search.", "新的职位推荐", "有五个软件工程职位符合你保存的搜索条件。"),
    ("social", "You have six unread notifications", "Open the social application to view your unread notifications.", "你有六条未读动态", "打开社交应用即可查看六条未读通知。"),
]


def support_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for category, scenarios in SUPPORT_SCENARIOS.items():
        for index, scenario in enumerate(scenarios, start=1):
            slug, risk, sources, zh_subject, zh_body, en_subject, en_body = scenario
            for language, subject, body in (
                ("zh", zh_subject, zh_body),
                ("en", en_subject, en_body),
            ):
                rows.append(
                    {
                        "id": f"ext-{category}-{language}-{index:02d}",
                        "language": language,
                        "customer_name": f"Eval Customer {category[:3].upper()}{index:02d}",
                        "customer_email": f"eval-{category}-{index}@example.com",
                        "subject": subject,
                        "body": body,
                        "expected_support": True,
                        "expected_category": category,
                        "expected_risk": risk,
                        "expected_sources": sources,
                        "tags": [category, slug, language, "extended"],
                    }
                )
    return rows


def non_support_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, (slug, en_subject, en_body, zh_subject, zh_body) in enumerate(
        NON_SUPPORT_SCENARIOS,
        start=1,
    ):
        for language, subject, body in (
            ("zh", zh_subject, zh_body),
            ("en", en_subject, en_body),
        ):
            rows.append(
                {
                    "id": f"ext-nonsupport-{language}-{index:02d}",
                    "language": language,
                    "customer_name": "Automated Sender",
                    "customer_email": f"noreply-{slug}@notifications.example.com",
                    "subject": subject,
                    "body": body,
                    "expected_support": False,
                    "expected_category": "other",
                    "expected_risk": "low",
                    "expected_sources": [],
                    "tags": ["non_support", slug, language, "extended"],
                }
            )
    return rows


def main() -> None:
    current_rows = [
        json.loads(line)
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(current_rows) == TARGET_CASE_COUNT:
        print(f"Dataset already contains {TARGET_CASE_COUNT} cases.")
        return
    if len(current_rows) != BASE_CASE_COUNT:
        raise ValueError(
            f"Expected {BASE_CASE_COUNT} base cases or {TARGET_CASE_COUNT} final cases, "
            f"found {len(current_rows)}."
        )

    rows = current_rows + support_rows() + non_support_rows()
    ids = [str(row["id"]) for row in rows]
    if len(rows) != TARGET_CASE_COUNT or len(ids) != len(set(ids)):
        raise ValueError("Extended dataset size or case IDs are invalid.")

    DATASET_PATH.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(f"Extended dataset from {BASE_CASE_COUNT} to {len(rows)} cases.")


if __name__ == "__main__":
    main()
