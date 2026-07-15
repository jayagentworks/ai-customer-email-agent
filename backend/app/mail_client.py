"""QQ 邮箱 IMAP/SMTP 接入模块。

这个模块只负责“邮件系统边界”：
- 通过 IMAP 拉取 QQ 邮箱中的邮件，并解析主题、发件人、正文和附件。
- 通过 SMTP 发送经过人工确认的回复。
- 对 MIME 标题、正文编码、附件内容做兼容处理，尽量减少中文乱码。

业务分类、RAG、回复生成不放在这里，避免邮箱协议解析和 Agent 决策耦合。
"""

import imaplib
import html
import os
import re
import smtplib
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from pathlib import Path
from email import message_from_bytes
from email.header import decode_header
from email.message import Message
from email.mime.text import MIMEText
from email.utils import parseaddr

from dotenv import load_dotenv

from app.document_processor import clean_text, parse_document
from app.models import EmailAttachment, EmailCreate

load_dotenv()
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class MailClientConfigError(Exception):
    """邮箱配置缺失或无效时抛出的异常。"""

    pass


@dataclass
class ImportedMail:
    """从邮箱服务商拉取到的一封邮件。

    ``provider_message_id`` 用于去重，避免同一封邮件被重复导入系统。
    """

    payload: EmailCreate
    provider_message_id: str


def fetch_unread_qq_emails(limit: int = 5, known_message_ids: set[str] | None = None) -> list[ImportedMail]:
    """从 QQ 邮箱拉取最近的邮件。

    当前默认搜索条件是 ``ALL``，再结合 ``known_message_ids`` 做本地去重。
    使用 ``BODY.PEEK[]`` 是为了读取邮件内容时不主动改变邮箱中的已读状态。
    """
    address = required_env("QQ_EMAIL_ADDRESS")
    auth_code = required_env("QQ_EMAIL_AUTH_CODE")
    host = os.getenv("QQ_IMAP_HOST", "imap.qq.com")
    port = int(os.getenv("QQ_IMAP_PORT", "993"))
    timeout = float(os.getenv("QQ_IMAP_TIMEOUT_SECONDS", "20"))
    search_criteria = os.getenv("QQ_IMAP_SEARCH_CRITERIA", "ALL")

    with imaplib.IMAP4_SSL(host, port, timeout=timeout) as client:
        client.login(address, auth_code)
        client.select("INBOX")
        status, data = client.search(None, search_criteria)
        if status != "OK":
            return []

        ids = data[0].split()[-limit:]
        imported: list[ImportedMail] = []
        for mail_id in reversed(ids):
            provider_message_id = fetch_provider_message_id(client, mail_id)
            if provider_message_id and known_message_ids and provider_message_id in known_message_ids:
                continue

            fetch_status, fetch_data = client.fetch(mail_id, "(BODY.PEEK[])")
            if fetch_status != "OK" or not fetch_data:
                continue

            raw_message = next(
                (part[1] for part in fetch_data if isinstance(part, tuple) and len(part) >= 2),
                None,
            )
            if not raw_message:
                continue

            message = message_from_bytes(raw_message)
            sender_name, sender_email = parseaddr(decode_mime_text(message.get("From", "")))
            body = extract_plain_text(message)
            if len(body.strip()) < 10:
                body = "(empty email body)"
            imported.append(
                ImportedMail(
                    payload=EmailCreate(
                        customer_name=sender_name or sender_email or "QQ Mail User",
                        customer_email=sender_email,
                        subject=decode_mime_text(message.get("Subject", "(no subject)")),
                        body=body,
                        attachments=extract_attachments(message),
                    ),
                    provider_message_id=provider_message_id or message.get("Message-ID", mail_id.decode("utf-8", errors="ignore")),
                )
            )

        return imported


def fetch_provider_message_id(client: imaplib.IMAP4_SSL, mail_id: bytes) -> str:
    """读取邮件 Message-ID，作为跨同步批次的稳定去重键。"""
    status, data = client.fetch(mail_id, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    if status != "OK" or not data:
        return mail_id.decode("utf-8", errors="ignore")
    raw_header = next((part[1] for part in data if isinstance(part, tuple) and len(part) >= 2), b"")
    if not raw_header:
        return mail_id.decode("utf-8", errors="ignore")
    message = message_from_bytes(raw_header)
    return message.get("Message-ID", mail_id.decode("utf-8", errors="ignore"))


def send_qq_email(*, to_address: str, subject: str, body: str) -> None:
    """通过 QQ SMTP 发送纯文本客服回复。"""
    from_address = required_env("QQ_EMAIL_ADDRESS")
    auth_code = required_env("QQ_EMAIL_AUTH_CODE")
    host = os.getenv("QQ_SMTP_HOST", "smtp.qq.com")
    port = int(os.getenv("QQ_SMTP_PORT", "465"))

    message = MIMEText(body, "plain", "utf-8")
    message["From"] = from_address
    message["To"] = to_address
    message["Subject"] = subject

    with smtplib.SMTP_SSL(host, port) as client:
        client.login(from_address, auth_code)
        client.sendmail(from_address, [to_address], message.as_string())


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise MailClientConfigError(f"Missing required environment variable: {name}")
    return value


def decode_mime_text(value: str) -> str:
    """解析 MIME 编码的邮件标题或发件人名称。"""
    fragments = []
    for payload, charset in decode_header(value):
        if isinstance(payload, bytes):
            fragments.append(decode_bytes(payload, charset))
        else:
            fragments.append(payload)
    return repair_mojibake_text("".join(fragments).strip())


def extract_plain_text(message: Message) -> str:
    """从邮件中提取正文文本。

    多段邮件会同时收集 text/plain 和 text/html 候选，然后按解码质量评分选择
    最可靠的一版。这样可以避免“第一段 text/plain 是乱码，但 HTML 正文正常”
    时提前返回错误内容。
    """
    candidates: list[tuple[int, str]] = []
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = part.get_content_disposition()
            if disposition == "attachment" or content_type not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            text = clean_email_text(decode_bytes(payload, part.get_content_charset()))
            if text:
                candidates.append((score_email_text(text, content_type), text))
    else:
        payload = message.get_payload(decode=True)
        if payload:
            text = clean_email_text(decode_bytes(payload, message.get_content_charset()))
            if text:
                return text

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    return "(empty email body)"


def clean_email_text(text: str) -> str:
    if not text:
        return text
    cleaned = html.unescape(text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</p\s*>", "\n", cleaned)
    cleaned = re.sub(r"(?i)<li\s*>", "\n- ", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def decode_bytes(payload: bytes, charset: str | None = None) -> str:
    candidates = normalize_charset_candidates(charset)
    best_text = ""
    best_score = -1
    for candidate in candidates:
        try:
            decoded = payload.decode(candidate, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
        text = repair_mojibake_text(decoded)
        score = score_decoded_text(text)
        if score > best_score:
            best_text = text
            best_score = score
    if best_text:
        return best_text
    return repair_mojibake_text(payload.decode("utf-8", errors="replace"))


def repair_mojibake_text(text: str) -> str:
    if not text:
        return text
    best_text = text
    best_score = score_decoded_text(text)
    for source_encoding in ("utf-16le", "gb18030", "gbk", "big5", "latin-1", "cp1252"):
        try:
            candidate = text.encode(source_encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        score = score_decoded_text(candidate)
        if score > best_score + 10:
            best_text = candidate
            best_score = score
    return best_text


def mojibake_score(text: str) -> int:
    markers = (
        "锟", "閿", "�", "Ã", "Â", "¤", "¥", "¢", "ä", "å", "æ", "ç", "è", "é", "½", "¡",
        "浣", "犲", "ソ",
        "鏂", "鎬", "煡", "鐪", "閫", "氱", "洿", "规", "嵁", "涓", "湪",
        "娆", "瀹", "㈡", "湇", "绛", "伐", "佷", "叆", "妯",
    )
    marker_score = sum(text.count(marker) for marker in markers)
    latin1_run_score = len(re.findall(r"[ÃÂ][\x80-\xffA-Za-z]{1,}", text))
    utf8_as_latin1_score = len(re.findall(r"[äåæçèé][\x80-\xffA-Za-z]{1,}", text))
    cjk_mojibake_run_score = len(re.findall(r"[浣犲ソ鏂鎬煡鐪嬫湁涓侀氱洿规嵁瀹㈡湇]{2,}", text))
    return marker_score + latin1_run_score * 3 + utf8_as_latin1_score * 3 + cjk_mojibake_run_score * 2


def utf16_ascii_pair_score(text: str) -> int:
    suspicious = 0
    for char in text:
        code = ord(char)
        low = code & 0xFF
        high = code >> 8
        if 32 <= low <= 126 and 32 <= high <= 126:
            suspicious += 1
    return suspicious


def normalize_charset_candidates(charset: str | None) -> list[str]:
    candidates: list[str] = []
    if charset:
        normalized = charset.strip().strip('"').lower()
        aliases = {
            "gb2312": ["gb18030", "gbk", "gb2312"],
            "gb_2312-80": ["gb18030", "gbk", "gb2312"],
            "x-gbk": ["gb18030", "gbk"],
            "cp936": ["gb18030", "gbk", "cp936"],
        }
        candidates.extend(aliases.get(normalized, [normalized]))
    candidates.extend(["utf-8", "gb18030", "gbk", "big5", "utf-16", "latin-1"])
    return list(dict.fromkeys(candidates))


def score_decoded_text(text: str) -> int:
    if not text:
        return -100
    replacement_penalty = text.count("�") * 20 + text.count("\x00") * 10
    question_run_penalty = text.count("????") * 10
    utf16_pair_penalty = utf16_ascii_pair_score(text) * 80
    mojibake_penalty = mojibake_score(text) * 35
    chinese_bonus = sum(1 for char in text if "\u4e00" <= char <= "\u9fff") * 2
    readable_bonus = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
    ascii_bonus = sum(1 for char in text if char.isascii() and (char.isalnum() or char in " .,;:/-_@<>")) * 2
    return readable_bonus + chinese_bonus + ascii_bonus - replacement_penalty - question_run_penalty - utf16_pair_penalty - mojibake_penalty


def score_email_text(text: str, content_type: str) -> int:
    text_type_bonus = 50 if content_type == "text/plain" else 0
    length_bonus = min(len(text), 1000) // 10
    return score_decoded_text(text) + text_type_bonus + length_bonus


def extract_attachments(message: Message) -> list[EmailAttachment]:
    if not message.is_multipart():
        return []

    attachments: list[EmailAttachment] = []
    for part in message.walk():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not filename and disposition != "attachment":
            continue

        payload = part.get_payload(decode=True) or b""
        decoded_filename = decode_mime_text(filename or "attachment")
        content_type = part.get_content_type()
        attachments.append(build_attachment(decoded_filename, content_type, payload, part))
    return attachments


def build_attachment(filename: str, content_type: str, payload: bytes, part: Message) -> EmailAttachment:
    filename = repair_mojibake_text(filename)
    preview, parse_status, status_message, parse_report = parse_attachment_payload(filename, content_type, payload, part)
    return EmailAttachment(
        filename=filename,
        content_type=content_type,
        size_bytes=len(payload),
        text_preview=preview,
        parse_status=parse_status,
        status_message=status_message,
        parse_report=parse_report,
    )


def parse_attachment_payload(filename: str, content_type: str, payload: bytes, part: Message) -> tuple[str, str, str, dict]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".pdf", ".docx", ".doc"} or content_type in {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
        return parse_document_attachment(filename, content_type, payload)

    text_preview = extract_text_attachment_preview(filename, content_type, part, payload)
    if text_preview:
        return text_preview, "parsed", "Text attachment parsed into the email context.", {
            "file_type": suffix.lstrip(".") or content_type,
            "parser": "mail-text",
            "cleaned_chars": len(text_preview),
        }
    return "", "metadata_only", "Attachment metadata captured; content parsing is not supported for this file type.", {}


def parse_document_attachment(filename: str, content_type: str, payload: bytes) -> tuple[str, str, str, dict]:
    suffix = infer_document_suffix(filename, content_type)
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / f"attachment{suffix or '.bin'}"
        path.write_bytes(payload)
        try:
            parsed = parse_document(path)
        except ValueError as exc:
            return "", "failed", str(exc), {
                "file_type": suffix.lstrip(".") or "unknown",
                "parser": "document_processor",
                "warnings": [str(exc)],
            }

    preview = build_document_preview(parsed.chunks)
    return preview, "parsed", f"Parsed {len(parsed.chunks)} chunk(s) from attachment.", parsed.report.to_dict()


def infer_document_suffix(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix:
        return suffix
    return {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    }.get(content_type, "")


def build_document_preview(chunks) -> str:
    text = "\n\n".join(chunk.content for chunk in chunks)
    return text[:3000].strip()


def extract_text_attachment_preview(filename: str, content_type: str, part: Message, payload: bytes) -> str:
    content_type = part.get_content_type()
    filename = filename.lower()
    text_like_suffixes = (".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".log")
    if not (
        content_type.startswith("text/")
        or content_type in {"application/json", "application/xml"}
        or filename.endswith(text_like_suffixes)
    ):
        return ""

    text = decode_bytes(payload, part.get_content_charset())
    return clean_text(text)[:3000]
