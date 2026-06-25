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
from app.workflow import process_email, regenerate_draft_reply, strip_internal_reference_lines

app = FastAPI(title="Customer Email Agent API")

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
    return {"message": "Customer Email Agent API is alive"}


@app.get("/emails", response_model=list[EmailRecord])
def list_emails() -> list[EmailRecord]:
    return store.list()


@app.get("/emails/{email_id}", response_model=EmailRecord)
def get_email(email_id: str) -> EmailRecord:
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


@app.post("/emails/process", response_model=EmailRecord)
def create_and_process_email(payload: EmailCreate) -> EmailRecord:
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
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")

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
    try:
        imported = fetch_unread_qq_emails(limit=limit)
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
    email = store.get(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    if not email.draft_reply:
        raise HTTPException(status_code=400, detail="Draft reply is empty")
    if email.status != "ready_to_send":
        raise HTTPException(status_code=400, detail="Email must be approved before sending")

    try:
        email.draft_reply = strip_internal_reference_lines(email.draft_reply)
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
    return list_knowledge_documents()


@app.get("/operation-logs", response_model=list[OperationLog])
def get_operation_logs(limit: int = Query(default=100, ge=1, le=300)) -> list[OperationLog]:
    return list_operation_logs(limit=limit)


@app.delete("/operation-logs/cleanup")
def cleanup_logs(
    retention_days: int = Query(default=180, ge=7, le=730),
    scope: str | None = Query(default=None),
) -> dict[str, int]:
    deleted = cleanup_operation_logs(retention_days=retention_days, scope=scope)
    return {"deleted": deleted}


@app.delete("/emails/workflow-steps/cleanup")
def cleanup_email_workflow_steps(retention_days: int = Query(default=30, ge=7, le=365)) -> dict[str, int]:
    deleted = store.cleanup_workflow_steps(retention_days=retention_days)
    return {"deleted": deleted}


@app.post("/knowledge/documents", response_model=KnowledgeDocument)
def create_knowledge_base_document(payload: KnowledgeDocumentCreate) -> KnowledgeDocument:
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
