"""FastAPI 接口入口。

这个文件只负责 HTTP API 编排：
- 邮件相关接口：创建/同步/处理/审核/发送。
- 知识库相关接口：上传、增删改查、版本回退、重新索引。
- 运行日志接口：展示和清理邮件 Agent 轨迹、知识库操作日志。

真正的业务逻辑分别下沉到 ``workflow.py``、``knowledge.py``、``mail_client.py``
和 ``store.py``，这样接口层保持薄而清晰。
"""

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.knowledge import (
    cleanup_operation_logs,
    create_knowledge_document,
    create_knowledge_document_upload_job_with_duplicate_policy,
    delete_knowledge_document,
    delete_knowledge_document_version,
    get_knowledge_document,
    ingest_knowledge_documents,
    index_knowledge_document,
    list_knowledge_document_versions,
    list_knowledge_documents,
    list_operation_logs,
    record_operation_log,
    reindex_knowledge_document,
    restore_knowledge_document_version,
    search_knowledge,
    update_knowledge_document,
    update_knowledge_document_from_upload,
)
from app.mail_client import MailClientConfigError, fetch_unread_qq_emails, send_qq_email
from app.models import (
    EmailCreate,
    EmailRecord,
    KnowledgeDocument,
    KnowledgeDocumentCreate,
    KnowledgeDocumentDetail,
    KnowledgeDocumentUpdate,
    KnowledgeDocumentVersion,
    KnowledgeHit,
    KnowledgeSearchRequest,
    OperationLog,
    ReviewAction,
)
from app.store import EmailStore
from app.workflow import process_email, regenerate_draft_reply, sanitize_customer_reply

app = FastAPI(title="Customer Email Agent API")

# 本项目的前端是本地 Vite 应用，因此只开放本地开发端口的跨域访问。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

store = EmailStore()


@app.get("/")
def health() -> dict[str, str]:
    """健康检查接口，用于确认后端服务已启动。"""
    return {"message": "Customer Email Agent API is alive"}


# ------------------------- 邮件处理接口 -------------------------


@app.get("/emails", response_model=list[EmailRecord])
def list_emails() -> list[EmailRecord]:
    """返回系统中的邮件列表。"""
    return store.list()


@app.get("/emails/{email_id}", response_model=EmailRecord)
def get_email(email_id: str) -> EmailRecord:
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


@app.post("/emails/process", response_model=EmailRecord)
def create_and_process_email(payload: EmailCreate) -> EmailRecord:
    """创建一封邮件并立即执行 Agent 工作流。"""
    email = store.create(payload)
    processed = process_email(email)
    saved = store.save(processed)
    record_operation_log(
        scope="email",
        action="process_email",
        title=saved.subject,
        summary=f"邮件「{saved.subject}」已完成 Agent 分类、检索和回复草稿生成。",
        detail={
            "email_id": saved.id,
            "customer_email": saved.customer_email,
            "category": saved.category,
            "confidence": saved.confidence,
            "status": saved.status,
            "attachment_count": len(saved.attachments),
        },
    )
    return saved


@app.post("/emails/{email_id}/review", response_model=EmailRecord)
def review_email(email_id: str, payload: ReviewAction) -> EmailRecord:
    """人工审核邮件草稿。

    审核动作包括通过、要求修改、升级处理和撤销升级。每次审核都会写入
    review history，方便前端展示人工操作记录。
    """
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

    if payload.action == "approve":
        email.status = "ready_to_send"
        email.review_note = payload.note or default_review_note(email.detected_language, "approve")
    elif payload.action == "revise":
        email.status = "needs_revision"
        email.review_note = payload.note or default_review_note(email.detected_language, "revise")
        if payload.revised_reply:
            email.draft_reply = payload.revised_reply
    elif payload.action == "escalate":
        email.status = "escalated"
        email.review_note = payload.note or default_review_note(email.detected_language, "escalate")
    else:
        email.status = "human_review"
        email.review_note = payload.note or default_review_note(email.detected_language, "undo_escalate")

    saved = store.save(email)
    store.record_review(email_id, payload)
    record_operation_log(
        scope="email",
        action=f"review_{payload.action}",
        title=email.subject,
        summary=f"邮件「{email.subject}」审核动作为 {payload.action}，当前状态为 {saved.status}。",
        detail={
            "email_id": email.id,
            "customer_email": email.customer_email,
            "action": payload.action,
            "status": saved.status,
            "has_revised_reply": bool(payload.revised_reply),
        },
    )
    return store.get(email_id) or saved


@app.post("/emails/{email_id}/draft/regenerate", response_model=EmailRecord)
def regenerate_email_draft(email_id: str) -> EmailRecord:
    """重新生成当前邮件的回复草稿。"""
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    if email.status in {"new", "irrelevant"} or not email.category or email.category == "other":
        raise HTTPException(status_code=400, detail="Email must be processed as a customer support request before regenerating a draft")

    saved = store.save(regenerate_draft_reply(email))
    record_operation_log(
        scope="email",
        action="regenerate_draft",
        title=email.subject,
        summary=f"邮件「{email.subject}」已重新生成回复草稿。",
        detail={
            "email_id": email.id,
            "customer_email": email.customer_email,
            "status": saved.status,
        },
    )
    return store.get(email_id) or saved


@app.post("/mail/qq/import")
def import_qq_mail(background_tasks: BackgroundTasks, limit: int = Query(default=10, ge=1, le=50)) -> dict:
    """同步 QQ 邮箱并把新邮件加入后台处理队列。

    接口返回时不一定已经完成 Agent 分析，因为每封新邮件会通过 BackgroundTasks
    异步调用 ``process_imported_email``。这样前端可以先看到“已同步，处理中”，
    再轮询刷新最终分类结果。
    """
    try:
        known_message_ids = store.list_provider_message_ids("qq", limit=300)
        imported = fetch_unread_qq_emails(limit=limit, known_message_ids=known_message_ids)
    except MailClientConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    queued_emails: list[EmailRecord] = []
    skipped_count = 0
    for item in imported:
        if store.exists_provider_message("qq", item.provider_message_id):
            skipped_count += 1
            continue
        email = EmailRecord(
            **item.payload.model_dump(),
            provider="qq",
            provider_message_id=item.provider_message_id,
        )
        email.status = "new"
        email.review_note = "已同步，等待 Agent 后台处理。"
        saved = store.save(email)
        queued_emails.append(saved)
        background_tasks.add_task(process_imported_email, saved.id)

    record_operation_log(
        scope="mail",
        action="import_qq_mail",
        title="QQ 邮箱同步",
        summary=f"QQ 邮箱同步完成，新增 {len(queued_emails)} 封邮件进入后台 Agent 处理。",
        detail={
            "requested_limit": limit,
            "queued_count": len(queued_emails),
            "skipped_count": skipped_count,
            "provider": "qq",
        },
    )

    return {
        "queued_count": len(queued_emails),
        "skipped_count": skipped_count,
        "emails": queued_emails,
    }


def process_imported_email(email_id: str) -> None:
    """后台处理从 QQ 邮箱导入的邮件。"""
    email = store.get(email_id)
    if not email:
        return
    processed = process_email(email)
    saved = store.save(processed)
    record_operation_log(
        scope="email",
        action="process_imported_email",
        title=saved.subject,
        summary=f"邮件「{saved.subject}」已完成后台 Agent 处理。",
        detail={
            "email_id": saved.id,
            "customer_email": saved.customer_email,
            "category": saved.category,
            "confidence": saved.confidence,
            "status": saved.status,
        },
    )


@app.delete("/mail/qq/corrupted")
def delete_corrupted_qq_mail() -> dict[str, int]:
    """清理历史乱码邮件记录，方便重新从邮箱同步。"""
    deleted_count = store.delete_corrupted_provider_messages("qq")
    record_operation_log(
        scope="mail",
        action="delete_corrupted_qq_mail",
        title="QQ 邮箱乱码清理",
        summary=f"已清理 {deleted_count} 封已损坏编码的 QQ 邮件记录，可重新同步原邮件。",
        detail={
            "deleted_count": deleted_count,
            "provider": "qq",
        },
    )
    return {"deleted_count": deleted_count}


@app.post("/emails/{email_id}/send", response_model=EmailRecord)
def send_email_reply(email_id: str) -> EmailRecord:
    """发送经过人工确认的回复。

    只有 ``ready_to_send`` 状态的邮件允许发送，防止低置信度或未审核草稿被误发。
    """
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    if not email.draft_reply:
        raise HTTPException(status_code=400, detail="Draft reply is empty")
    if email.status != "ready_to_send":
        raise HTTPException(status_code=400, detail="Email must be approved before sending")

    try:
        email.draft_reply = sanitize_customer_reply(email.draft_reply)
        send_qq_email(
            to_address=email.customer_email,
            subject=f"Re: {email.subject}",
            body=email.draft_reply,
        )
    except MailClientConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    email.status = "sent"
    email.review_note = "已通过 QQ SMTP 发送回复。" if email.detected_language == "zh" else "Reply sent via QQ SMTP."
    saved = store.save(email)
    record_operation_log(
        scope="mail",
        action="send_reply",
        title=email.subject,
        summary=f"邮件「{email.subject}」已通过 QQ SMTP 发送回复。",
        detail={
            "email_id": email.id,
            "to": email.customer_email,
            "subject": f"Re: {email.subject}",
            "status": saved.status,
        },
    )
    return saved


@app.get("/knowledge/documents", response_model=list[KnowledgeDocument])
def get_knowledge_documents() -> list[KnowledgeDocument]:
    """查询知识库文档列表。"""
    return list_knowledge_documents()


@app.get("/operation-logs", response_model=list[OperationLog])
def get_operation_logs(limit: int = Query(default=100, ge=1, le=300)) -> list[OperationLog]:
    """查询运行日志。"""
    return list_operation_logs(limit=limit)


@app.delete("/operation-logs/cleanup")
def cleanup_logs(
    retention_days: int = Query(default=180, ge=7, le=730),
    scope: str | None = Query(default=None),
) -> dict[str, int]:
    """清理过期运行日志，避免日志无限增长。"""
    deleted = cleanup_operation_logs(retention_days=retention_days, scope=scope)
    return {"deleted": deleted}


@app.delete("/emails/workflow-steps/cleanup")
def cleanup_email_workflow_steps(retention_days: int = Query(default=30, ge=7, le=365)) -> dict[str, int]:
    """清理邮件 Agent 执行轨迹。"""
    deleted = store.cleanup_workflow_steps(retention_days=retention_days)
    return {"deleted": deleted}


@app.post("/knowledge/documents", response_model=KnowledgeDocument)
def create_knowledge_base_document(payload: KnowledgeDocumentCreate) -> KnowledgeDocument:
    """手动创建知识库文档。"""
    return create_knowledge_document(payload)


@app.get("/knowledge/documents/{document_id}", response_model=KnowledgeDocumentDetail)
def get_knowledge_base_document(document_id: str) -> KnowledgeDocumentDetail:
    document = get_knowledge_document(document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return document


@app.get("/knowledge/documents/{document_id}/versions", response_model=list[KnowledgeDocumentVersion])
def get_knowledge_base_document_versions(document_id: str) -> list[KnowledgeDocumentVersion]:
    try:
        return list_knowledge_document_versions(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/knowledge/documents/{document_id}/versions/{version_id}/restore", response_model=KnowledgeDocument)
def restore_knowledge_base_document_version(document_id: str, version_id: str) -> KnowledgeDocument:
    try:
        return restore_knowledge_document_version(document_id, version_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/knowledge/documents/{document_id}/versions/{version_id}")
def delete_knowledge_base_document_version(document_id: str, version_id: str) -> dict[str, str]:
    try:
        delete_knowledge_document_version(document_id, version_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.put("/knowledge/documents/{document_id}", response_model=KnowledgeDocument)
def update_knowledge_base_document(document_id: str, payload: KnowledgeDocumentUpdate) -> KnowledgeDocument:
    try:
        return update_knowledge_document(document_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/knowledge/documents/{document_id}/reindex", response_model=KnowledgeDocument)
def reindex_knowledge_base_document(document_id: str) -> KnowledgeDocument:
    try:
        return reindex_knowledge_document(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/knowledge/documents/{document_id}")
def delete_knowledge_base_document(document_id: str) -> dict[str, str]:
    try:
        delete_knowledge_document(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.post("/knowledge/documents/upload", response_model=KnowledgeDocument)
async def upload_knowledge_base_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    force_weak_duplicate: bool = Query(default=False),
) -> KnowledgeDocument:
    content = await file.read()
    try:
        document = create_knowledge_document_upload_job_with_duplicate_policy(
            file.filename or "knowledge.md",
            content,
            force_weak_duplicate=force_weak_duplicate,
        )
        background_tasks.add_task(index_knowledge_document, document.id)
        return document
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail={"kind": "weak_duplicate", "message": str(exc)}) from exc
    except ValueError as exc:
        status_code = 409 if str(exc).startswith("强去重命中") else 400
        kind = "strong_duplicate" if status_code == 409 else "upload_error"
        raise HTTPException(status_code=status_code, detail={"kind": kind, "message": str(exc)}) from exc


@app.put("/knowledge/documents/{document_id}/upload", response_model=KnowledgeDocument)
async def upload_knowledge_base_document_revision(
    document_id: str,
    file: UploadFile = File(...),
    force_weak_duplicate: bool = Query(default=False),
) -> KnowledgeDocument:
    content = await file.read()
    try:
        return update_knowledge_document_from_upload(
            document_id,
            file.filename or "knowledge.md",
            content,
            force_weak_duplicate=force_weak_duplicate,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail={"kind": "weak_duplicate", "message": str(exc)}) from exc
    except ValueError as exc:
        status_code = 409 if str(exc).startswith("强去重命中") else 400
        kind = "strong_duplicate" if status_code == 409 else "upload_error"
        raise HTTPException(status_code=status_code, detail={"kind": kind, "message": str(exc)}) from exc


@app.post("/knowledge/ingest", response_model=list[KnowledgeDocument])
def ingest_knowledge_base() -> list[KnowledgeDocument]:
    return ingest_knowledge_documents()


@app.post("/knowledge/search", response_model=list[KnowledgeHit])
def search_knowledge_base(payload: KnowledgeSearchRequest) -> list[KnowledgeHit]:
    return search_knowledge(payload.query, category=payload.category, limit=payload.limit)


def default_review_note(language: str, action: str) -> str:
    if language == "zh":
        return {
            "approve": "审核通过，可以发送。",
            "revise": "审核要求修改回复草稿。",
            "escalate": "已升级给人工专员处理。",
            "undo_escalate": "已撤销升级，邮件回到人工审核队列。",
        }[action]
    return {
        "approve": "Approved by reviewer.",
        "revise": "Reviewer requested changes.",
        "escalate": "Escalated to a human specialist.",
        "undo_escalate": "Escalation undone; the email is back in human review.",
    }[action]
