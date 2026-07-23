"""知识库管理与 RAG 检索模块。

本模块负责把企业知识文件从“上传/手动录入”变成“可检索的知识片段”：
1. 文件入库前会做强去重和弱去重，避免重复文档污染检索结果。
2. 文档解析后会被清洗、切分成 chunk，并生成 embedding。
3. chunk 会同时存到 PostgreSQL 普通字段和 pgvector 向量字段中。
4. 邮件处理时先用 pgvector 快速召回候选，再用语义、关键词、分类三类分数重排。

这里没有直接把所有候选都交给 LLM，是为了控制 token 成本，并降低无关知识
进入上下文后造成幻觉的概率。
"""

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, text as sql_text
from sqlalchemy.orm import selectinload

from app.db import SessionLocal, engine, init_db
from app.db_models import KnowledgeChunkORM, KnowledgeDocumentORM, KnowledgeDocumentVersionORM, OperationLogORM
from app.document_processor import SUPPORTED_KNOWLEDGE_SUFFIXES, parse_document
from app.embedding_client import cosine_similarity, embed_text, embed_texts
from app.models import (
    KnowledgeDocument,
    KnowledgeDocumentCreate,
    KnowledgeDocumentDetail,
    KnowledgeDocumentUpdate,
    KnowledgeDocumentVersion,
    KnowledgeHit,
    OperationLog,
)

KNOWLEDGE_DOCS_DIR = Path(__file__).resolve().parents[1] / "knowledge_docs"
INDEXING_MESSAGE = "已完成清洗、切分和向量索引，可用于 RAG 检索。"
PROCESSING_MESSAGE = "正在解析文件、切分文本并生成向量索引。"


@dataclass
class DuplicateCandidate:
    """重复文档候选。

    强去重和弱去重都只把“疑似重复对象”记录成这个结构，真正是否阻止上传
    由调用方根据策略决定。
    """

    id: str
    title: str
    source: str
    reason: str


@dataclass
class UploadPrecheck:
    """上传前预检查结果。

    strong_duplicates：内容哈希完全一致，基本可以认为是同一份文档。
    weak_duplicates：内容高度相似、文件名相同或标题接近，需要提示用户确认。
    """

    title: str
    content_hash: str
    strong_duplicates: list[DuplicateCandidate]
    weak_duplicates: list[DuplicateCandidate]


def ingest_knowledge_documents() -> list[KnowledgeDocument]:
    """扫描默认知识库目录并批量入库。

    这个接口主要用于初始化或重新索引：把 ``backend/knowledge_docs`` 下面
    已存在的 md/txt/pdf/docx/doc 文件统一解析并写入数据库。
    """
    init_db()
    KNOWLEDGE_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    documents: list[KnowledgeDocument] = []

    for path in sorted(KNOWLEDGE_DOCS_DIR.rglob("*")):
        if path.suffix.lower() not in SUPPORTED_KNOWLEDGE_SUFFIXES or not path.is_file():
            continue
        documents.append(upsert_document(path))

    return list_knowledge_documents()


def create_knowledge_document(payload: KnowledgeDocumentCreate) -> KnowledgeDocument:
    """创建手动录入的知识库文档。

    手动录入会落成一个 markdown 文件，然后复用 ``upsert_document`` 走同一套
    解析、切分、embedding、版本记录逻辑，避免“上传文档”和“手敲内容”两套索引路径不一致。
    """
    init_db()
    target_dir = KNOWLEDGE_DOCS_DIR / "custom"
    target_dir.mkdir(parents=True, exist_ok=True)
    source = safe_source_name(payload.source or payload.title)
    path = target_dir / source
    content = payload.content.strip()
    if not content.startswith("#"):
        content = f"# {payload.title.strip()}\n\n{content}"
    path.write_text(content, encoding="utf-8")
    return upsert_document(path)


def create_knowledge_document_from_upload(filename: str, content: bytes) -> KnowledgeDocument:
    init_db()
    path = save_uploaded_knowledge_file(filename, content)
    return upsert_document(path)


def create_knowledge_document_upload_job(filename: str, content: bytes) -> KnowledgeDocument:
    init_db()
    precheck = precheck_uploaded_knowledge_file(filename, content)
    if precheck.strong_duplicates:
        candidate = precheck.strong_duplicates[0]
        raise ValueError(f"强去重命中：该文件内容已入库，现有文档「{candidate.title}」（{candidate.source}）。")
    if precheck.weak_duplicates:
        candidate = precheck.weak_duplicates[0]
        raise RuntimeError(f"弱去重提醒：该文件可能已入库，疑似文档「{candidate.title}」（{candidate.source}）。")
    path = save_uploaded_knowledge_file(filename, content)
    source = path.relative_to(KNOWLEDGE_DOCS_DIR).as_posix()
    document_id = mark_document_processing(source, path.stem)
    return get_document_model(document_id)


def create_knowledge_document_upload_job_with_duplicate_policy(filename: str, content: bytes, force_weak_duplicate: bool = False) -> KnowledgeDocument:
    """创建上传任务，并应用强/弱去重策略。

    - 强去重命中：直接拒绝，因为内容完全一致。
    - 弱去重命中：默认提醒用户；如果前端传入 ``force_weak_duplicate``，
      表示用户已确认仍然入库。
    """
    init_db()
    precheck = precheck_uploaded_knowledge_file(filename, content)
    ensure_upload_not_duplicate(precheck, force_weak_duplicate=force_weak_duplicate)
    path = save_uploaded_knowledge_file(filename, content)
    source = path.relative_to(KNOWLEDGE_DOCS_DIR).as_posix()
    document_id = mark_document_processing(source, path.stem)
    return get_document_model(document_id)


def update_knowledge_document_from_upload(
    document_id: str,
    filename: str,
    content: bytes,
    force_weak_duplicate: bool = False,
) -> KnowledgeDocument:
    """用上传文件更新已有知识库文档。

    这里会先排除当前文档自身，再执行弱去重检查，避免“上传当前文档的新版本”
    被误判为与自己重复。真正写入后仍然通过 ``upsert_document`` 创建版本快照。
    """
    init_db()
    with SessionLocal() as session:
        row = session.get(KnowledgeDocumentORM, document_id)
        if row is None:
            raise ValueError("Knowledge document not found.")

    precheck = precheck_uploaded_knowledge_file(filename, content, exclude_weak_document_id=document_id)
    ensure_upload_not_duplicate(precheck, force_weak_duplicate=force_weak_duplicate)
    path = save_uploaded_knowledge_file(filename, content)
    return upsert_document(
        path,
        document_id=document_id,
        version_note=f"Updated from uploaded file {Path(filename).name}.",
    )


def index_knowledge_document(document_id: str) -> None:
    """后台索引单个知识库文档。

    上传接口会先创建一条“处理中”的文档记录，随后由后台任务调用本函数。
    这样前端不会因为大文件解析、OCR 或 embedding 调用而长时间卡住。
    """
    init_db()
    try:
        with SessionLocal() as session:
            row = session.get(KnowledgeDocumentORM, document_id)
            if row is None:
                return
            source = row.source

        path = resolve_source_path(source)
        if not path.exists() or not path.is_file():
            mark_document_failed(document_id, "Source file is missing.")
            return
        upsert_document(path)
    except Exception as exc:
        mark_document_failed(document_id, str(exc))


def save_uploaded_knowledge_file(filename: str, content: bytes) -> Path:
    """保存上传的原始知识文件。

    原文件保留下来是为了支持后续重新索引、版本回退和解析报告复查。
    文件名会做安全化处理，避免路径穿越和特殊字符导致的跨平台问题。
    """
    original_name = Path(filename).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in SUPPORTED_KNOWLEDGE_SUFFIXES:
        raise ValueError("Only .md, .txt, .pdf, .docx and .doc knowledge files are supported now.")
    source = safe_source_name(original_name)

    if len(content) < 20:
        raise ValueError("Knowledge file content is too short.")

    target_dir = KNOWLEDGE_DOCS_DIR / "uploads"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = unique_upload_path(target_dir / source)
    path.write_bytes(content)
    return path


def unique_upload_path(path: Path) -> Path:
    """为同名上传文件生成不冲突的保存路径。"""
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError("Too many uploaded files share the same filename. Please rename the file and upload again.")


def precheck_uploaded_knowledge_file(filename: str, content: bytes, exclude_weak_document_id: str | None = None) -> UploadPrecheck:
    """上传前解析并计算重复度。

    强去重使用 SHA-256：只要清洗后的正文完全一样，哈希值就完全一致。
    弱去重使用文本 shingle 相似度：即使用户改了文件名、调整了少量文字，
    只要主体内容高度相似，也会提醒用户可能已经入库。
    """
    original_name = Path(filename).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in SUPPORTED_KNOWLEDGE_SUFFIXES:
        raise ValueError("Only .md, .txt, .pdf, .docx and .doc knowledge files are supported now.")
    if len(content) < 20:
        raise ValueError("Knowledge file content is too short.")

    with tempfile.TemporaryDirectory(prefix="knowledge-upload-check-") as temp_dir:
        temp_path = Path(temp_dir) / safe_source_name(original_name)
        temp_path.write_bytes(content)
        parsed = parse_document(temp_path)

    cleaned = "\n\n".join(chunk.content for chunk in parsed.chunks if chunk.content.strip()).strip()
    if len(cleaned) < 20:
        raise ValueError("Knowledge file content is too short after parsing.")
    content_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    title = parsed.title or Path(original_name).stem
    safe_name = safe_source_name(original_name)
    source_stem = Path(safe_name).stem.lower()
    title_key = normalize_duplicate_key(title)
    cleaned_key = normalize_content_for_similarity(cleaned)
    upload_shingles = build_text_shingles(cleaned_key)

    strong: list[DuplicateCandidate] = []
    weak: list[DuplicateCandidate] = []
    with SessionLocal() as session:
        rows = session.scalars(
            select(KnowledgeDocumentORM).options(selectinload(KnowledgeDocumentORM.chunks))
        ).all()
        for row in rows:
            if row.content_hash and row.content_hash == content_hash:
                strong.append(DuplicateCandidate(row.id, row.title, row.source, "content_hash"))
                continue
            if exclude_weak_document_id and row.id == exclude_weak_document_id:
                continue

            existing_text = "\n\n".join(chunk.content for chunk in row.chunks if chunk.content.strip())
            existing_key = normalize_content_for_similarity(existing_text)
            similarity = content_similarity(cleaned_key, existing_key, upload_shingles)
            if similarity >= 0.92:
                weak.append(DuplicateCandidate(row.id, row.title, row.source, f"content_similarity:{similarity:.2f}"))
                continue

            row_source_stem = Path(row.source).stem.lower()
            row_title_key = normalize_duplicate_key(row.title)
            if row_source_stem == source_stem:
                weak.append(DuplicateCandidate(row.id, row.title, row.source, "same_filename"))
            elif title_key and row_title_key and (title_key in row_title_key or row_title_key in title_key):
                weak.append(DuplicateCandidate(row.id, row.title, row.source, "similar_title"))

    return UploadPrecheck(title=title, content_hash=content_hash, strong_duplicates=strong, weak_duplicates=weak)


def ensure_upload_not_duplicate(precheck: UploadPrecheck, force_weak_duplicate: bool = False) -> None:
    """根据预检查结果决定是否允许继续上传。"""
    if precheck.strong_duplicates:
        candidate = precheck.strong_duplicates[0]
        raise ValueError(f"强去重命中：该文件内容已入库，现有文档「{candidate.title}」（{candidate.source}）。")
    if precheck.weak_duplicates and not force_weak_duplicate:
        candidate = precheck.weak_duplicates[0]
        raise RuntimeError(f"弱去重提醒：该文件可能已入库，疑似文档「{candidate.title}」（{candidate.source}）。")


def normalize_duplicate_key(value: str) -> str:
    """归一化标题/文件名，用于弱去重的近似比较。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def normalize_content_for_similarity(value: str) -> str:
    """归一化正文内容，去掉空白和标点，保留中英文与数字主体。"""
    compact = re.sub(r"\s+", "", value.lower())
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", compact)


def build_text_shingles(value: str, size: int = 12) -> set[str]:
    """把文本切成固定长度片段，用于弱去重相似度计算。

    例如一篇文档只改了一个字，大多数 shingle 仍然会重合，因此相似度仍然很高。
    这比只比较文件名或标题更可靠。
    """
    if not value:
        return set()
    if len(value) <= size:
        return {value}
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def content_similarity(upload_key: str, existing_key: str, upload_shingles: set[str]) -> float:
    """计算上传文档与已有文档的内容相似度。

    先处理“短文本完全包含在长文本里”的场景，再用 Jaccard 形式比较 shingle
    集合重合比例。返回值越接近 1，说明两份文档越相似。
    """
    if not upload_key or not existing_key:
        return 0.0
    shorter, longer = sorted((upload_key, existing_key), key=len)
    if shorter and shorter in longer:
        return len(shorter) / len(longer)

    existing_shingles = build_text_shingles(existing_key)
    if not upload_shingles or not existing_shingles:
        return 0.0
    overlap = len(upload_shingles & existing_shingles)
    union = len(upload_shingles | existing_shingles)
    return overlap / union if union else 0.0


def list_knowledge_documents() -> list[KnowledgeDocument]:
    init_db()
    with SessionLocal() as session:
        rows = session.scalars(
            select(KnowledgeDocumentORM)
            .options(selectinload(KnowledgeDocumentORM.chunks), selectinload(KnowledgeDocumentORM.versions))
            .order_by(KnowledgeDocumentORM.created_at.desc())
        ).all()
        return [
            build_document_model(row, len(row.chunks))
            for row in rows
        ]


def get_knowledge_document(document_id: str) -> KnowledgeDocumentDetail | None:
    init_db()
    with SessionLocal() as session:
        row = session.scalar(
            select(KnowledgeDocumentORM)
            .options(selectinload(KnowledgeDocumentORM.chunks), selectinload(KnowledgeDocumentORM.versions))
            .where(KnowledgeDocumentORM.id == document_id)
        )
        if row is None:
            return None
        content = read_source_file(row.source)
        return KnowledgeDocumentDetail(
            id=row.id,
            title=row.title,
            source=row.source,
            chunk_count=len(row.chunks),
            current_version=max((version.version_number for version in row.versions), default=0),
            status=row.status,
            status_message=row.status_message,
            parse_report=row.parse_report or {},
            created_at=row.created_at,
            content=content,
        )


def update_knowledge_document(document_id: str, payload: KnowledgeDocumentUpdate) -> KnowledgeDocument:
    """更新知识库正文，并生成新版本。

    这里会覆盖源文件并重新走 ``upsert_document``。版本快照由 upsert 统一创建，
    因此修改、上传新版本、回退后的重新索引都能保持相同的版本语义。
    """
    init_db()
    with SessionLocal() as session:
        row = session.get(KnowledgeDocumentORM, document_id)
        if row is None:
            raise ValueError("Knowledge document not found.")
        source = row.source

    path = resolve_source_path(source)
    content = payload.content.strip()
    if not content.startswith("#"):
        content = f"# {payload.title.strip()}\n\n{content}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return upsert_document(path)


def reindex_knowledge_document(document_id: str) -> KnowledgeDocument:
    """重新解析并索引已有知识库文档。"""
    init_db()
    with SessionLocal() as session:
        row = session.get(KnowledgeDocumentORM, document_id)
        if row is None:
            raise ValueError("Knowledge document not found.")
        source = row.source

    path = resolve_source_path(source)
    if not path.exists() or not path.is_file():
        mark_document_failed(document_id, "Source file is missing.")
        return get_document_model(document_id)
    return upsert_document(path)


def restore_knowledge_document_version(document_id: str, version_id: str) -> KnowledgeDocument:
    """回退到指定历史版本。

    当前实现采用“恢复为目标版本，并删除目标版本之后的版本记录”的语义，
    这样用户界面中不会出现回退后版本号继续膨胀的问题。回退行为会写入运行日志，
    方便审计。
    """
    init_db()
    with SessionLocal() as session:
        row = session.get(KnowledgeDocumentORM, document_id)
        version = session.get(KnowledgeDocumentVersionORM, version_id)
        if row is None or version is None or version.document_id != document_id:
            raise ValueError("Knowledge document version not found.")
        snapshot = version.content_snapshot.strip()
        if len(snapshot) < 20:
            raise ValueError("This version does not contain a restorable content snapshot.")
        version_number = version.version_number
        title = version.title

    restored_dir = KNOWLEDGE_DOCS_DIR / "restored"
    restored_dir.mkdir(parents=True, exist_ok=True)
    restored_path = restored_dir / f"{document_id}-from-v{version_number}.md"
    restored_path.write_text(snapshot, encoding="utf-8")
    restored_source = restored_path.relative_to(KNOWLEDGE_DOCS_DIR).as_posix()

    with SessionLocal() as session:
        row = session.get(KnowledgeDocumentORM, document_id)
        if row is None:
            raise ValueError("Knowledge document not found.")
        row.source = restored_source
        row.updated_at = datetime.utcnow()
        session.execute(
            delete(KnowledgeDocumentVersionORM).where(
                KnowledgeDocumentVersionORM.document_id == document_id,
                KnowledgeDocumentVersionORM.version_number > version_number,
            )
        )
        session.commit()

    restored_document = upsert_document(
        restored_path,
        version_note=f"Restored from version v{version_number}.",
        create_version=False,
    )
    record_operation_log(
        scope="knowledge",
        action="restore_version",
        title=title,
        summary=f"\u77e5\u8bc6\u5e93\u300c{title}\u300d\u5df2\u56de\u9000\u5230 v{version_number}\u3002",
        detail={
            "document_id": document_id,
            "restored_from_version": version_number,
            "current_version": restored_document.current_version,
            "restored_source": restored_document.source,
        },
    )
    return restored_document


def delete_knowledge_document_version(document_id: str, version_id: str) -> None:
    """删除某个历史版本。

    当前版本不能直接删除，避免用户把正在使用的知识内容删掉后导致检索依据消失。
    如果需要替换当前版本，应先回退到其他版本，再删除旧历史版本。
    """
    init_db()
    with SessionLocal() as session:
        document = session.get(KnowledgeDocumentORM, document_id)
        version = session.get(KnowledgeDocumentVersionORM, version_id)
        if document is None or version is None or version.document_id != document_id:
            raise ValueError("Knowledge document version not found.")

        latest_version = session.scalar(
            select(func.max(KnowledgeDocumentVersionORM.version_number)).where(
                KnowledgeDocumentVersionORM.document_id == document_id
            )
        )
        if version.version_number == latest_version:
            raise ValueError("Current version cannot be deleted. Restore another version first if you need to replace it.")

        deleted_version_number = version.version_number
        deleted_title = version.title
        session.delete(version)
        session.commit()

    record_operation_log(
        scope="knowledge",
        action="delete_version",
        title=deleted_title,
        summary=f"知识库「{deleted_title}」已删除历史版本 v{deleted_version_number}。",
        detail={
            "document_id": document_id,
            "deleted_version": deleted_version_number,
        },
    )


def delete_knowledge_document(document_id: str) -> None:
    """删除知识库文档及其数据库记录。"""
    init_db()
    source = ""
    with SessionLocal() as session:
        row = session.get(KnowledgeDocumentORM, document_id)
        if row is None:
            raise ValueError("Knowledge document not found.")
        source = row.source
        session.delete(row)
        session.commit()

    path = resolve_source_path(source)
    if path.exists() and path.is_file():
        path.unlink()


SEMANTIC_RERANK_WEIGHT = 0.48
KEYWORD_RERANK_WEIGHT = 0.34
CATEGORY_RERANK_WEIGHT = 0.18

ENGLISH_SEARCH_STOP_WORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "before",
    "but",
    "can",
    "could",
    "does",
    "for",
    "from",
    "has",
    "have",
    "help",
    "how",
    "into",
    "need",
    "our",
    "please",
    "should",
    "that",
    "the",
    "their",
    "this",
    "through",
    "was",
    "what",
    "when",
    "where",
    "which",
    "will",
    "with",
    "would",
    "your",
}
CHINESE_SEARCH_STOP_WORDS = {
    "一个",
    "一些",
    "什么",
    "可以",
    "如何",
    "客户",
    "客服",
    "已经",
    "我们",
    "是否",
    "系统",
    "请问",
    "这个",
    "需要",
    "问题",
}
CROSS_LANGUAGE_TERM_GROUPS = (
    {"password", "reset", "expired", "密码", "重置", "过期"},
    {"login", "sign in", "access", "workspace", "登录", "访问", "工作区"},
    {"verification code", "code", "验证码"},
    {"oauth", "authorization", "permission", "permissions", "授权", "权限"},
    {"mfa", "multi factor", "多因素认证"},
    {
        "incident",
        "outage",
        "timeout",
        "timed out",
        "unavailable",
        "production",
        "blocked",
        "error",
        "status page",
        "故障",
        "中断",
        "超时",
        "不可用",
        "生产",
        "受阻",
        "错误",
        "状态页",
    },
    {"team", "members", "affected", "impact", "团队", "成员", "影响"},
    {"complaint", "unhappy", "dissatisfied", "投诉", "不满"},
    {"supervisor", "escalate", "priority", "主管", "升级", "优先级"},
    {"follow up", "repeated", "催促", "多次来信"},
    {"sla", "response time", "响应时间", "时效"},
    {"privacy", "personal data", "data export", "deletion", "隐私", "个人数据", "数据导出", "删除"},
    {"refund", "duplicate charge", "charged twice", "退款", "重复扣费"},
    {"authorization hold", "pending charge", "preauthorization", "预授权", "待处理扣款"},
    {"renewal", "cancelled", "cancellation", "续费", "取消订阅"},
    {"invoice", "billing", "receipt", "发票", "账单", "收据"},
    {"tax id", "invoice title", "credit note", "税号", "发票抬头", "红冲"},
    {"purchase order", "vendor", "quotation", "procurement", "采购订单", "供应商", "报价单", "采购"},
    {"plan", "pro", "feature", "套餐", "功能", "版本"},
    {"role", "roles", "read only", "角色", "只读"},
    {"audit log", "audit", "审计日志", "审计"},
    {"integration", "webhook", "api", "集成", "接口"},
    {"contract", "pricing", "discount", "合同", "报价", "折扣"},
)


def retrieve_knowledge(text: str, category: str | None = None, limit: int = 3) -> list[KnowledgeHit]:
    """RAG 检索入口。

    检索分两步：
    1. 用 pgvector 根据邮件 query embedding 召回一批候选 chunk。
    2. 在应用层做轻量重排：语义分 48%、关键词分 34%、分类分 18%。

    这里默认返回 Top-3，是因为回复生成真正需要的是少量高质量依据。
    Top-K 太大虽然召回更宽，但会增加 token 成本，也更容易把无关知识塞给 LLM。
    """
    init_db()
    ensure_default_documents()
    backfill_pgvector_embeddings()
    query_embedding, _ = embed_text(build_query(text, category))
    normalize_embedding_dimensions(len(query_embedding))
    candidate_ids = pgvector_candidate_ids(query_embedding, category, limit)

    with SessionLocal() as session:
        statement = select(KnowledgeChunkORM).join(KnowledgeDocumentORM).where(KnowledgeDocumentORM.status == "indexed")
        if candidate_ids:
            candidate_rows = session.scalars(statement.where(KnowledgeChunkORM.id.in_(candidate_ids))).all()
            row_by_id = {row.id: row for row in candidate_rows}
            rows = [row_by_id[row_id] for row_id in candidate_ids if row_id in row_by_id]
        elif category and category != "other":
            category_rows = session.scalars(statement.where(KnowledgeChunkORM.category == category)).all()
            rows = category_rows or session.scalars(statement).all()
        else:
            rows = session.scalars(statement).all()

    scored: list[tuple[float, KnowledgeChunkORM, float, float, float, str]] = []
    query_terms = expand_cross_language_terms(extract_terms(text))
    for row in rows:
        vector_score = cosine_similarity(query_embedding, row.embedding or [])
        searchable_content = f"{row.title}\n{row.source}\n{row.content}"
        keyword_score = keyword_overlap(query_terms, searchable_content)
        category_score = category_alignment_score(
            category,
            query_terms,
            row.category,
            searchable_content,
        )
        score = max(
            0.0,
            min(
                0.99,
                vector_score * SEMANTIC_RERANK_WEIGHT
                + keyword_score * KEYWORD_RERANK_WEIGHT
                + category_score * CATEGORY_RERANK_WEIGHT,
            ),
        )
        if category_score > 0 and (keyword_score > 0 or vector_score > 0):
            score = max(score, 0.24)
        if score >= 0.18:
            scored.append((score, row, vector_score, keyword_score, category_score, build_match_reason(vector_score, keyword_score, category_score)))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        KnowledgeHit(
            title=row.title,
            source=row.source,
            snippet=compact_snippet(row.content),
            score=round(score, 2),
            semantic_score=round(vector_score, 2),
            keyword_score=round(keyword_score, 2),
            category_score=round(category_score, 2),
            category=row.category,
            match_reason=match_reason,
            reliability=knowledge_reliability(score),
            page_number=row.page_number,
            section_title=row.section_title,
        )
        for score, row, vector_score, keyword_score, category_score, match_reason in scored[:limit]
    ]


def search_knowledge(query: str, category: str | None = None, limit: int = 5) -> list[KnowledgeHit]:
    return retrieve_knowledge(query, category=category, limit=limit)


def normalize_embedding_dimensions(expected_dimensions: int, limit: int = 100) -> None:
    """修复历史 chunk 的 embedding 维度不一致问题。

    如果切换过 embedding 模型，旧 chunk 的向量维度可能和新 query 向量不一致。
    pgvector 在维度不一致时无法正确比较，因此这里会检测并重算一批不匹配的向量。
    """
    if expected_dimensions <= 0:
        return

    with SessionLocal() as session:
        rows = session.scalars(
            select(KnowledgeChunkORM)
            .join(KnowledgeDocumentORM)
            .where(KnowledgeDocumentORM.status == "indexed")
            .limit(limit)
        ).all()

    mismatched = [row for row in rows if embedding_dimensions(row.embedding) != expected_dimensions]
    if not mismatched:
        return

    texts = [row.content for row in mismatched]
    embeddings, embedding_model = embed_texts(texts)
    chunk_vectors: list[tuple[str, list[float]]] = []
    with SessionLocal() as session:
        for row, embedding in zip(mismatched, embeddings):
            if len(embedding) != expected_dimensions:
                continue
            db_row = session.get(KnowledgeChunkORM, row.id)
            if not db_row:
                continue
            db_row.embedding = embedding
            db_row.embedding_model = embedding_model
            chunk_vectors.append((row.id, embedding))
        session.commit()
    sync_pgvector_embeddings(chunk_vectors)


def embedding_dimensions(embedding: object) -> int:
    """兼容 JSON 字符串和 Python list 两种历史存储形态，返回向量维度。"""
    if isinstance(embedding, str):
        try:
            embedding = json.loads(embedding)
        except json.JSONDecodeError:
            return 0
    if isinstance(embedding, list):
        return len(embedding)
    return 0


def pgvector_enabled() -> bool:
    """判断当前数据库是否支持 pgvector 检索。"""
    return engine.dialect.name == "postgresql"


def vector_literal(vector: list[float]) -> str:
    """把 Python 向量序列化为 pgvector 可接收的文本形式。"""
    return json.dumps([round(float(value), 6) for value in vector], separators=(",", ":"))


def pgvector_candidate_ids(query_embedding: list[float], category: str | None, limit: int) -> list[str]:
    """使用 pgvector 从数据库侧召回候选 chunk id。

    数据库只负责做高效向量近邻召回；最终排序仍在 Python 侧融合关键词和分类分数。
    这样既利用了 pgvector 的索引能力，又保留了业务可解释的混合检索逻辑。
    """
    if not pgvector_enabled() or not query_embedding:
        return []

    candidate_limit = max(limit * 8, 24)
    base_sql = """
        SELECT knowledge_chunks.id
        FROM knowledge_chunks
        JOIN knowledge_documents ON knowledge_documents.id = knowledge_chunks.document_id
        WHERE knowledge_documents.status = 'indexed'
          AND knowledge_chunks.embedding_vector IS NOT NULL
          AND vector_dims(knowledge_chunks.embedding_vector) = :dimensions
    """
    params = {
        "query_embedding": vector_literal(query_embedding),
        "dimensions": len(query_embedding),
        "limit": candidate_limit,
    }
    if category and category != "other":
        base_sql += " AND knowledge_chunks.category = :category"
        params["category"] = category
    base_sql += """
        ORDER BY knowledge_chunks.embedding_vector <=> CAST(:query_embedding AS vector)
        LIMIT :limit
    """

    try:
        with engine.connect() as connection:
            return [row[0] for row in connection.execute(sql_text(base_sql), params).all()]
    except Exception:
        return []


def sync_pgvector_embeddings(chunk_vectors: list[tuple[str, list[float]]]) -> None:
    if not pgvector_enabled() or not chunk_vectors:
        return

    try:
        with engine.begin() as connection:
            for chunk_id, embedding in chunk_vectors:
                if not chunk_id or not embedding:
                    continue
                connection.execute(
                    sql_text(
                        "UPDATE knowledge_chunks "
                        "SET embedding_vector = CAST(:embedding AS vector) "
                        "WHERE id = :chunk_id"
                    ),
                    {"embedding": vector_literal(embedding), "chunk_id": chunk_id},
                )
    except Exception:
        # Keep JSON embeddings as the source of truth if pgvector is not
        # available for this database instance.
        return


def backfill_pgvector_embeddings(limit: int = 1000) -> None:
    if not pgvector_enabled():
        return

    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sql_text(
                    "SELECT id, embedding FROM knowledge_chunks "
                    "WHERE embedding_vector IS NULL AND embedding IS NOT NULL "
                    "LIMIT :limit"
                ),
                {"limit": limit},
            ).all()
    except Exception:
        return

    chunk_vectors: list[tuple[str, list[float]]] = []
    for chunk_id, embedding in rows:
        if isinstance(embedding, str):
            try:
                embedding = json.loads(embedding)
            except json.JSONDecodeError:
                embedding = []
        if isinstance(embedding, list) and embedding:
            chunk_vectors.append((chunk_id, embedding))
    sync_pgvector_embeddings(chunk_vectors)


def upsert_document(
    path: Path,
    document_id: str | None = None,
    version_note: str = "Indexed from uploaded or edited source file.",
    create_version: bool = True,
) -> KnowledgeDocument:
    source = path.relative_to(KNOWLEDGE_DOCS_DIR).as_posix()
    document_id = mark_document_processing(source, path.stem, document_id=document_id)

    try:
        parsed = parse_document(path)
        title = parsed.title
        chunk_texts = [chunk.content for chunk in parsed.chunks if chunk.content.strip()]
        cleaned = "\n\n".join(chunk_texts)
        if len(cleaned.strip()) < 20:
            raise ValueError("Knowledge file content is too short after parsing.")
        content_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
        embeddings, embedding_model = embed_texts(chunk_texts)
    except Exception as exc:
        mark_document_failed(document_id, str(exc))
        return get_document_model(document_id)

    now = datetime.utcnow()
    with SessionLocal() as session:
        row = session.get(KnowledgeDocumentORM, document_id)
        if row is None:
            raise ValueError("Knowledge document not found during indexing.")
        row.title = title
        row.content_hash = content_hash
        row.status = "indexed"
        row.status_message = INDEXING_MESSAGE
        row.parse_report = parsed.report.to_dict()
        row.updated_at = now
        session.execute(delete(KnowledgeChunkORM).where(KnowledgeChunkORM.document_id == row.id))
        session.flush()

        pending_chunks: list[tuple[KnowledgeChunkORM, list[float]]] = []
        for index, (chunk, embedding) in enumerate(zip(parsed.chunks, embeddings)):
            chunk_row = KnowledgeChunkORM(
                document_id=row.id,
                position=index,
                title=title,
                source=f"{source}#chunk-{index + 1}",
                category=infer_category(f"{title}\n{chunk.content}", source=source),
                content=chunk.content,
                token_estimate=estimate_tokens(chunk.content),
                page_number=chunk.page_number,
                section_title=chunk.section_title,
                embedding_model=embedding_model,
                embedding=embedding,
            )
            session.add(chunk_row)
            pending_chunks.append((chunk_row, embedding))
        session.flush()
        pending_vectors = [(chunk_row.id, embedding) for chunk_row, embedding in pending_chunks]
        if create_version:
            create_document_version(
                session=session,
                row=row,
                chunk_count=len(parsed.chunks),
                content_snapshot=cleaned,
                note=version_note,
            )
        session.commit()
        sync_pgvector_embeddings(pending_vectors)
        session.refresh(row)
        return build_document_model(row, len(parsed.chunks))


def mark_document_processing(source: str, fallback_title: str, document_id: str | None = None) -> str:
    now = datetime.utcnow()
    with SessionLocal() as session:
        row = session.get(KnowledgeDocumentORM, document_id) if document_id else None
        if row is None:
            row = session.scalar(select(KnowledgeDocumentORM).where(KnowledgeDocumentORM.source == source))
        if row is None:
            row = KnowledgeDocumentORM(
                title=extract_title(fallback_title, fallback_title),
                source=source,
                content_hash="",
                status="processing",
                status_message=PROCESSING_MESSAGE,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
        else:
            row.source = source
            row.status = "processing"
            row.status_message = PROCESSING_MESSAGE
            row.parse_report = {}
            row.updated_at = now
        session.commit()
        return row.id


def mark_document_failed(document_id: str, reason: str) -> None:
    with SessionLocal() as session:
        row = session.get(KnowledgeDocumentORM, document_id)
        if row is None:
            return
        row.status = "failed"
        row.status_message = compact_snippet(reason, max_chars=220) or "索引失败，请检查文件内容后重试。"
        row.updated_at = datetime.utcnow()
        session.commit()


def create_document_version(
    session,
    row: KnowledgeDocumentORM,
    chunk_count: int,
    content_snapshot: str,
    note: str = "",
) -> bool:
    latest_row = session.scalar(
        select(KnowledgeDocumentVersionORM)
        .where(KnowledgeDocumentVersionORM.document_id == row.id)
        .order_by(KnowledgeDocumentVersionORM.version_number.desc())
    )
    if (
        latest_row is not None
        and latest_row.content_hash == row.content_hash
        and latest_row.title == row.title
        and latest_row.chunk_count == chunk_count
    ):
        return False

    session.add(
        KnowledgeDocumentVersionORM(
            document_id=row.id,
            version_number=((latest_row.version_number if latest_row else 0) + 1),
            title=row.title,
            source=row.source,
            content_hash=row.content_hash,
            content_snapshot=content_snapshot,
            chunk_count=chunk_count,
            status=row.status,
            status_message=row.status_message,
            parse_report=row.parse_report or {},
            note=note,
        )
    )
    return True


def list_knowledge_document_versions(document_id: str) -> list[KnowledgeDocumentVersion]:
    init_db()
    with SessionLocal() as session:
        exists = session.get(KnowledgeDocumentORM, document_id)
        if exists is None:
            raise ValueError("Knowledge document not found.")
        rows = session.scalars(
            select(KnowledgeDocumentVersionORM)
            .where(KnowledgeDocumentVersionORM.document_id == document_id)
            .order_by(KnowledgeDocumentVersionORM.version_number.desc())
        ).all()
        return [build_version_model(row) for row in rows]


def list_operation_logs(limit: int = 100) -> list[OperationLog]:
    init_db()
    with SessionLocal() as session:
        rows = session.scalars(
            select(OperationLogORM)
            .order_by(OperationLogORM.created_at.desc())
            .limit(limit)
        ).all()
        return [build_operation_log_model(row) for row in rows]


def cleanup_operation_logs(retention_days: int = 180, scope: str | None = None) -> int:
    init_db()
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    with SessionLocal() as session:
        statement = delete(OperationLogORM).where(OperationLogORM.created_at < cutoff)
        if scope:
            statement = statement.where(OperationLogORM.scope == scope)
        result = session.execute(statement)
        session.commit()
        return result.rowcount or 0


def record_operation_log(scope: str, action: str, title: str, summary: str, detail: dict | None = None) -> None:
    with SessionLocal() as session:
        session.add(
            OperationLogORM(
                scope=scope,
                action=action,
                title=title,
                summary=summary,
                detail=detail or {},
            )
        )
        session.commit()


def build_document_model(row: KnowledgeDocumentORM, chunk_count: int) -> KnowledgeDocument:
    current_version = max((version.version_number for version in row.versions), default=0)
    if current_version == 0:
        with SessionLocal() as session:
            current_version = session.scalar(
                select(func.max(KnowledgeDocumentVersionORM.version_number)).where(
                    KnowledgeDocumentVersionORM.document_id == row.id
                )
            ) or 0
    return KnowledgeDocument(
        id=row.id,
        title=row.title,
        source=row.source,
        chunk_count=chunk_count,
        current_version=current_version,
        status=row.status,
        status_message=row.status_message,
        parse_report=row.parse_report or {},
        created_at=row.created_at,
    )


def build_version_model(row: KnowledgeDocumentVersionORM) -> KnowledgeDocumentVersion:
    return KnowledgeDocumentVersion(
        id=row.id,
        document_id=row.document_id,
        version_number=row.version_number,
        title=row.title,
        source=row.source,
        content_hash=row.content_hash,
        content_snapshot=row.content_snapshot,
        chunk_count=row.chunk_count,
        status=row.status,
        status_message=row.status_message,
        parse_report=row.parse_report or {},
        note=row.note,
        created_at=row.created_at,
    )


def build_operation_log_model(row: OperationLogORM) -> OperationLog:
    return OperationLog(
        id=row.id,
        scope=row.scope,
        action=row.action,
        title=row.title,
        summary=row.summary,
        detail=row.detail or {},
        created_at=row.created_at,
    )


def get_document_model(document_id: str) -> KnowledgeDocument:
    with SessionLocal() as session:
        row = session.scalar(
            select(KnowledgeDocumentORM)
            .options(selectinload(KnowledgeDocumentORM.chunks), selectinload(KnowledgeDocumentORM.versions))
            .where(KnowledgeDocumentORM.id == document_id)
        )
        if row is None:
            raise ValueError("Knowledge document not found.")
        return build_document_model(row, len(row.chunks))


def ensure_default_documents() -> None:
    init_db()
    with SessionLocal() as session:
        count = session.scalar(select(func.count()).select_from(KnowledgeDocumentORM))
    if count == 0:
        ingest_knowledge_documents()


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_into_chunks(text: str, max_chars: int = 700) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    chunks: list[str] = []
    current = ""

    for block in blocks:
        if current and len(current) + len(block) + 2 > max_chars:
            chunks.append(current.strip())
            current = block
        else:
            current = f"{current}\n\n{block}".strip() if current else block

    if current:
        chunks.append(current.strip())
    return chunks or [text]


def extract_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip("# ").strip()
        if stripped:
            return stripped[:120]
    return fallback.replace("_", " ").replace("-", " ").title()


def infer_category(text: str, source: str = "") -> str:
    lower = f"{source}\n{text}".lower()
    source_rules = [
        ("refund", {"refund"}),
        ("technical", {"login", "troubleshooting"}),
        ("complaint", {"escalation", "playbook"}),
        ("product_question", {"product", "faq"}),
    ]
    for category, markers in source_rules:
        if any(marker in lower for marker in markers):
            return category

    rules = [
        ("refund", {"refund", "duplicate charge", "charged twice", "退款", "重复扣费", "重复收费"}),
        ("technical", {"login", "password", "token", "sso", "登录", "密码", "无法访问", "重置"}),
        ("complaint", {"complaint", "cancel", "legal", "blocked", "投诉", "取消", "法律", "律师", "阻塞"}),
        ("billing", {"invoice", "billing", "receipt", "tax", "发票", "账单", "收据", "税务"}),
        ("product_question", {"feature", "plan", "integration", "workspace", "功能", "套餐", "版本", "集成"}),
    ]
    for category, keywords in rules:
        if any(keyword in lower for keyword in keywords):
            return category
    return "other"


def build_query(text: str, category: str | None) -> str:
    expanded_terms = " ".join(sorted(category_keyword_terms(category)))
    return f"category: {category or 'unknown'}\n{expanded_terms}\n{text}"


def extract_terms(text: str) -> set[str]:
    lower = text.lower()
    english_tokens = [
        token
        for token in re.findall(r"[a-z0-9_]+", lower)
        if (len(token) >= 3 or token in {"api", "sso", "mfa"})
        and token not in ENGLISH_SEARCH_STOP_WORDS
    ]
    terms = set(english_tokens)
    # 完整短语比单个常见词更能区分“密码重置”和“服务事故”等子意图。
    for size in (2, 3):
        terms.update(
            " ".join(english_tokens[index : index + size])
            for index in range(0, max(0, len(english_tokens) - size + 1))
        )
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", lower)
    for run in chinese_runs:
        for size in (2, 3, 4):
            terms.update(
                term
                for index in range(0, max(0, len(run) - size + 1))
                if (term := run[index : index + size]) not in CHINESE_SEARCH_STOP_WORDS
            )
    return terms


def expand_cross_language_terms(query_terms: set[str]) -> set[str]:
    """按业务同义词组补齐中英文词项，支持跨语言邮件检索中文知识库。"""
    expanded = set(query_terms)
    for group in CROSS_LANGUAGE_TERM_GROUPS:
        if query_terms & group:
            expanded.update(group)
    return expanded


def category_keyword_terms(category: str | None) -> set[str]:
    terms_by_category = {
        "product_question": {
            "product",
            "feature",
            "plan",
            "pro",
            "workspace",
            "team",
            "permission",
            "permissions",
            "audit",
            "logs",
            "integration",
            "integrations",
            "sso",
            "webhook",
            "api",
            "role",
            "roles",
            "read only",
            "priority support",
            "compliance",
            "pricing",
            "contract",
            "产品",
            "功能",
            "套餐",
            "版本",
            "团队",
            "工作区",
            "协作",
            "权限",
            "管理",
            "审计",
            "日志",
            "集成",
            "角色",
            "只读",
            "优先支持",
            "合规",
            "报价",
            "合同",
        },
        "refund": {
            "refund",
            "duplicate",
            "charge",
            "charged",
            "billing",
            "invoice",
            "payment",
            "authorization",
            "renewal",
            "cancelled",
            "cancellation",
            "original payment",
            "退款",
            "退费",
            "重复",
            "扣费",
            "收费",
            "账单",
            "支付",
            "发票",
            "预授权",
            "续费",
            "取消订阅",
            "原路退回",
        },
        "billing": {
            "invoice",
            "billing",
            "receipt",
            "tax",
            "payment",
            "title",
            "vendor",
            "quotation",
            "purchase order",
            "contract",
            "bank transfer",
            "账单",
            "发票",
            "收据",
            "税务",
            "支付",
            "抬头",
            "供应商",
            "报价单",
            "采购订单",
            "合同",
            "转账",
        },
        "technical": {
            "login",
            "password",
            "token",
            "sso",
            "access",
            "workspace",
            "reset",
            "verification code",
            "oauth",
            "mfa",
            "timeout",
            "incident",
            "outage",
            "production",
            "error",
            "status page",
            "登录",
            "密码",
            "重置",
            "访问",
            "工作区",
            "令牌",
            "验证码",
            "多因素认证",
            "超时",
            "故障",
            "服务异常",
            "生产环境",
            "错误码",
            "状态页",
        },
        "complaint": {
            "complaint",
            "unhappy",
            "cancel",
            "legal",
            "blocked",
            "escalate",
            "supervisor",
            "incident",
            "outage",
            "production",
            "sla",
            "repeated",
            "follow up",
            "privacy",
            "data export",
            "投诉",
            "不满",
            "取消",
            "法律",
            "律师",
            "升级",
            "主管",
            "故障",
            "中断",
            "生产",
            "多次催促",
            "隐私",
            "数据导出",
        },
    }
    return terms_by_category.get(category or "", set())


def keyword_overlap(query_terms: set[str], content: str) -> float:
    if not query_terms:
        return 0.0
    lower = content.lower()
    matched_weights = sorted(
        (search_term_weight(term) for term in query_terms if term in lower),
        reverse=True,
    )
    # 只累计最有辨识度的六个词项，避免长邮件凭借大量普通词自然取得满分。
    return min(1.0, sum(matched_weights[:6]) / 8.0)


def category_alignment_score(
    category: str | None,
    query_terms: set[str],
    row_category: str | None,
    content: str,
) -> float:
    """计算分类一致性，并用类别内子意图词区分同类文档。

    pgvector 候选通常已经按粗类别过滤，因此旧版二元分类分在同类候选之间完全相同。
    这里保留 0.65 的粗类别基础分，再根据邮件与文档共同命中的类别关键词增加最多
    0.35，使“技术问题”内部还能区分登录、OAuth、服务事故等具体方向。
    """
    if not category or category == "other" or row_category != category:
        return 0.0
    category_terms = category_keyword_terms(category)
    intent_terms = query_terms & category_terms
    if not intent_terms:
        return 0.65
    lower = content.lower()
    matched = sum(search_term_weight(term) for term in intent_terms if term in lower)
    total = sum(search_term_weight(term) for term in intent_terms)
    return min(1.0, 0.65 + 0.35 * safe_ratio(matched, total))


def search_term_weight(term: str) -> float:
    """给长词和完整短语更高权重，降低普通短词对排序的干扰。"""
    if " " in term:
        return min(2.4, 1.4 + 0.2 * len(term.split()))
    if re.fullmatch(r"[\u4e00-\u9fff]+", term):
        return {2: 0.8, 3: 1.1, 4: 1.4}.get(len(term), 1.4)
    if len(term) >= 9:
        return 1.4
    if len(term) >= 6:
        return 1.15
    return 0.8


def safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def build_match_reason(vector_score: float, keyword_score: float, category_score: float) -> str:
    reasons: list[str] = []
    if vector_score >= 0.55:
        reasons.append("语义相似度较高")
    elif vector_score >= 0.35:
        reasons.append("语义相关")
    if keyword_score >= 0.4:
        reasons.append("关键词重合")
    elif keyword_score > 0:
        reasons.append("存在少量关键词交集")
    if category_score > 0:
        reasons.append("邮件分类与知识分类一致")
    return "；".join(reasons) or "综合语义、关键词和分类信号命中"


def knowledge_reliability(score: float) -> str:
    """把 RAG 综合分映射为知识依据可信度层级。"""
    if score >= 0.6:
        return "strong"
    if score >= 0.35:
        return "medium"
    return "weak"


def estimate_tokens(text: str) -> int:
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_words = len(re.findall(r"[a-zA-Z0-9_]+", text))
    return chinese_chars + latin_words


def compact_snippet(text: str, max_chars: int = 260) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


def read_source_file(source: str) -> str:
    path = resolve_source_path(source)
    if not path.exists() or not path.is_file():
        return ""
    if path.suffix.lower() in {".md", ".txt"}:
        return path.read_text(encoding="utf-8")
    parsed = parse_document(path)
    return "\n\n".join(chunk.content for chunk in parsed.chunks)


def resolve_source_path(source: str) -> Path:
    root = KNOWLEDGE_DOCS_DIR.resolve()
    path = (root / source).resolve()
    if root != path and root not in path.parents:
        raise ValueError("Knowledge document path is outside the knowledge directory.")
    return path


def safe_source_name(value: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_\-\.\u4e00-\u9fff]+", "_", value.strip()).strip("._")
    if not stem:
        stem = "knowledge_document"
    if not stem.lower().endswith(tuple(SUPPORTED_KNOWLEDGE_SUFFIXES)):
        stem = f"{stem}.md"
    return stem[:120]
