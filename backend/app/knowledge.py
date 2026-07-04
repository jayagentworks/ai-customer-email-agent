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
    id: str
    title: str
    source: str
    reason: str


@dataclass
class UploadPrecheck:
    title: str
    content_hash: str
    strong_duplicates: list[DuplicateCandidate]
    weak_duplicates: list[DuplicateCandidate]


def ingest_knowledge_documents() -> list[KnowledgeDocument]:
    init_db()
    KNOWLEDGE_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    documents: list[KnowledgeDocument] = []

    for path in sorted(KNOWLEDGE_DOCS_DIR.rglob("*")):
        if path.suffix.lower() not in SUPPORTED_KNOWLEDGE_SUFFIXES or not path.is_file():
            continue
        documents.append(upsert_document(path))

    return list_knowledge_documents()


def create_knowledge_document(payload: KnowledgeDocumentCreate) -> KnowledgeDocument:
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
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError("Too many uploaded files share the same filename. Please rename the file and upload again.")


def precheck_uploaded_knowledge_file(filename: str, content: bytes, exclude_weak_document_id: str | None = None) -> UploadPrecheck:
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
    if precheck.strong_duplicates:
        candidate = precheck.strong_duplicates[0]
        raise ValueError(f"强去重命中：该文件内容已入库，现有文档「{candidate.title}」（{candidate.source}）。")
    if precheck.weak_duplicates and not force_weak_duplicate:
        candidate = precheck.weak_duplicates[0]
        raise RuntimeError(f"弱去重提醒：该文件可能已入库，疑似文档「{candidate.title}」（{candidate.source}）。")


def normalize_duplicate_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def normalize_content_for_similarity(value: str) -> str:
    compact = re.sub(r"\s+", "", value.lower())
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", compact)


def build_text_shingles(value: str, size: int = 12) -> set[str]:
    if not value:
        return set()
    if len(value) <= size:
        return {value}
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def content_similarity(upload_key: str, existing_key: str, upload_shingles: set[str]) -> float:
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


def retrieve_knowledge(text: str, category: str | None = None, limit: int = 3) -> list[KnowledgeHit]:
    init_db()
    ensure_default_documents()
    backfill_pgvector_embeddings()
    query_embedding, _ = embed_text(build_query(text, category))
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
    query_terms = extract_terms(text)
    for row in rows:
        vector_score = cosine_similarity(query_embedding, row.embedding or [])
        keyword_score = keyword_overlap(query_terms, row.content)
        category_score = 1.0 if category and row.category == category else 0.0
        score = max(0.0, min(0.99, vector_score * 0.55 + keyword_score * 0.35 + category_score * 0.10))
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
            page_number=row.page_number,
            section_title=row.section_title,
        )
        for score, row, vector_score, keyword_score, category_score, match_reason in scored[:limit]
    ]


def search_knowledge(query: str, category: str | None = None, limit: int = 5) -> list[KnowledgeHit]:
    return retrieve_knowledge(query, category=category, limit=limit)


def pgvector_enabled() -> bool:
    return engine.dialect.name == "postgresql"


def vector_literal(vector: list[float]) -> str:
    return json.dumps([round(float(value), 6) for value in vector], separators=(",", ":"))


def pgvector_candidate_ids(query_embedding: list[float], category: str | None, limit: int) -> list[str]:
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
    return f"category: {category or 'unknown'}\n{text}"


def extract_terms(text: str) -> set[str]:
    lower = text.lower()
    terms = set(re.findall(r"[a-z0-9_]+", lower))
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", lower)
    for run in chinese_runs:
        for size in (2, 3, 4):
            terms.update(run[index : index + size] for index in range(0, max(0, len(run) - size + 1)))
    return terms


def keyword_overlap(query_terms: set[str], content: str) -> float:
    if not query_terms:
        return 0.0
    lower = content.lower()
    matches = sum(1 for term in query_terms if term in lower)
    return min(1.0, matches / 5)


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
