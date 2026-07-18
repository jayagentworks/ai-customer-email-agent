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
from html.parser import HTMLParser
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


@dataclass(frozen=True)
class ImapSmtpSettings:
    """QQ/Gmail 共用的 IMAP 与 SMTP 连接参数。"""

    provider: str
    email_address: str
    credential: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    timeout: float = 20.0
    search_criteria: str = "ALL"


def fetch_unread_qq_emails(limit: int = 5, known_message_ids: set[str] | None = None) -> list[ImportedMail]:
    """从 QQ 邮箱拉取最近的邮件。

    当前默认搜索条件是 ``ALL``，再结合 ``known_message_ids`` 做本地去重。
    使用 ``BODY.PEEK[]`` 是为了读取邮件内容时不主动改变邮箱中的已读状态。
    """
    return fetch_imap_emails(
        ImapSmtpSettings(
            provider="qq",
            email_address=required_env("QQ_EMAIL_ADDRESS"),
            credential=required_env("QQ_EMAIL_AUTH_CODE"),
            imap_host=os.getenv("QQ_IMAP_HOST", "imap.qq.com"),
            imap_port=int(os.getenv("QQ_IMAP_PORT", "993")),
            smtp_host=os.getenv("QQ_SMTP_HOST", "smtp.qq.com"),
            smtp_port=int(os.getenv("QQ_SMTP_PORT", "465")),
            timeout=float(os.getenv("QQ_IMAP_TIMEOUT_SECONDS", "20")),
            search_criteria=os.getenv("QQ_IMAP_SEARCH_CRITERIA", "ALL"),
        ),
        limit=limit,
        known_message_ids=known_message_ids,
    )


def fetch_imap_emails(
    settings: ImapSmtpSettings,
    *,
    limit: int = 5,
    known_message_ids: set[str] | None = None,
) -> list[ImportedMail]:
    """从符合标准 IMAP 的邮箱读取最近邮件，不改变服务器已读状态。"""
    with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=settings.timeout) as client:
        client.login(settings.email_address, settings.credential)
        client.select("INBOX")
        status, data = client.search(None, settings.search_criteria)
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
                        customer_name=sender_name or sender_email or f"{settings.provider.upper()} Mail User",
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
    send_smtp_email(
        ImapSmtpSettings(
            provider="qq",
            email_address=required_env("QQ_EMAIL_ADDRESS"),
            credential=required_env("QQ_EMAIL_AUTH_CODE"),
            imap_host=os.getenv("QQ_IMAP_HOST", "imap.qq.com"),
            imap_port=int(os.getenv("QQ_IMAP_PORT", "993")),
            smtp_host=os.getenv("QQ_SMTP_HOST", "smtp.qq.com"),
            smtp_port=int(os.getenv("QQ_SMTP_PORT", "465")),
        ),
        to_address=to_address,
        subject=subject,
        body=body,
    )


def send_smtp_email(settings: ImapSmtpSettings, *, to_address: str, subject: str, body: str) -> None:
    """通过 SSL SMTP 发送纯文本邮件，适用于 QQ 和 Gmail。"""
    from_address = settings.email_address

    message = MIMEText(body, "plain", "utf-8")
    message["From"] = from_address
    message["To"] = to_address
    message["Subject"] = subject

    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=settings.timeout) as client:
        client.login(from_address, settings.credential)
        client.sendmail(from_address, [to_address], message.as_string())


def test_imap_smtp_connection(settings: ImapSmtpSettings) -> None:
    """验证收信与发信登录，不读取正文也不发送测试邮件。"""
    with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=settings.timeout) as client:
        client.login(settings.email_address, settings.credential)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise MailClientConfigError("IMAP inbox is not accessible")
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=settings.timeout) as client:
        client.login(settings.email_address, settings.credential)


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
            text = clean_email_text(decode_bytes(payload, part.get_content_charset() or detect_inline_charset(payload)))
            if text:
                candidates.append((score_email_text(text, content_type), text))
    else:
        payload = message.get_payload(decode=True)
        if payload:
            text = clean_email_text(decode_bytes(payload, message.get_content_charset() or detect_inline_charset(payload)))
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
    if looks_like_html(cleaned):
        cleaned = extract_visible_html_text(cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</p\s*>", "\n", cleaned)
    cleaned = re.sub(r"(?i)<li\s*>", "\n- ", cleaned)
    cleaned = re.sub(r"(?is)<(style|script|head|noscript|template)[^>]*>.*?</\1>", "\n", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = strip_css_preamble(cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def looks_like_html(text: str) -> bool:
    return bool(re.search(r"(?is)</?(html|body|div|table|style|p|br|span|a|meta|head)\b", text))


class VisibleTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"style", "script", "head", "noscript", "template"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag.lower() in {"br", "p", "div", "tr", "li", "table", "section", "article", "h1", "h2", "h3"}:
            self.parts.append("\n")
        if tag.lower() == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"style", "script", "head", "noscript", "template"} and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag.lower() in {"p", "div", "tr", "li", "table", "section", "article", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def extract_visible_html_text(text: str) -> str:
    parser = VisibleTextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return text
    visible = parser.text().strip()
    return visible or text


def strip_css_preamble(text: str) -> str:
    """清理被邮件客户端当作正文吐出来的 CSS 片段。

    有些营销邮件的 text/plain 部分会把 CSS reset、媒体查询或隐藏块拼到正文
    最前面，形如 ``*{box-sizing...}@media... Sign In Hi ...``。这些内容不
    属于客户可读正文，会干扰非客服复核和后续分类，因此在展示/分析前切掉。
    """
    cleaned = text.strip()
    css_signal = len(re.findall(r"[#.]?[A-Za-z0-9_-]+\s*\{|@\s*media|!important|mso-|box-sizing|font-size|line-height", cleaned[:3000], re.I))
    if css_signal < 4:
        return cleaned
    primary_anchors = [
        r"\bHi\s+[^,\n]{0,40},",
        r"\bHello\s+[^,\n]{0,40},",
        r"\bDear\s+[^,\n]{0,40},",
        r"你好[，,]",
    ]
    secondary_anchors = [
        r"\bSign In\b",
        r"\bView in browser\b",
    ]
    for anchors in (primary_anchors, secondary_anchors):
        indexes = []
        for pattern in anchors:
            match = re.search(pattern, cleaned, re.I)
            if match and match.start() > 40:
                indexes.append(match.start())
        if indexes:
            return cleaned[min(indexes):].strip()
    return cleaned


def detect_inline_charset(payload: bytes) -> str | None:
    """从 HTML/XML 前几 KB 内容中识别内嵌 charset。

    部分邮件没有在 MIME part 头上声明 charset，却在 HTML ``meta`` 标签里声明。
    这里先用 ASCII 忽略错误读取头部片段，只提取 charset 名称，不直接信任正文。
    """
    head = payload[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset=[\"']?([A-Za-z0-9._-]+)", head, flags=re.IGNORECASE)
    return match.group(1) if match else None


def decode_bytes(payload: bytes, charset: str | None = None) -> str:
    """把邮件字节解码成尽可能可靠的 Unicode 文本。

    邮件乱码反复出现的根因通常不是“完全没有 charset”，而是：
    - 邮件头声明了 GB2312/GBK，但正文实际是 UTF-8；
    - 邮件服务商或转发链路把 UTF-8 字节错误地按 GBK/Big5 解码；
    - HTML 正文和纯文本正文质量不同，第一段并不一定最可靠。

    因此这里不再简单信任声明编码，而是让多个编码候选同时竞争，
    用 ``score_decoded_text`` 选择最像真实自然语言、最不像乱码的结果。
    """
    best_text = ""
    best_score = -10**9
    for candidate in normalize_charset_candidates(charset):
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
    """修复常见邮件乱码。

    QQ/网易/企业邮箱转发链路里经常会出现两类问题：
    1. UTF-8 字节被 Latin1/CP1252 当成文本保存，例如 ``ä½ å¥½``；
    2. GBK/GB18030 中文先被 Latin1 当成文本，又在后续链路里被 UTF-8/Latin1 再错一次，
       例如 ``ÃÄÃ£`` 这类双层乱码。

    所以这里不只做一次转换，而是用小范围 BFS 生成多轮候选，再用文本质量评分选择
    最像真实中文/英文邮件、最不像乱码的一版。
    """
    if not text:
        return text

    original_score = score_decoded_text(text)
    strong_mojibake_signal = has_latin1_mojibake_signal(text) or mojibake_score(text) >= 3
    max_rounds = 3 if strong_mojibake_signal else 1
    candidates = {text}
    frontier = {text}

    # 第一组处理“错误文本其实是某种字节序列被 Latin1/CP1252 直接映射成了字符”的情况。
    source_encodings = ("latin-1", "cp1252")
    target_encodings = ("utf-8", "utf-8-sig", "gb18030", "gbk", "gb2312", "big5")

    for _ in range(max_rounds):
        next_frontier: set[str] = set()
        for item in frontier:
            for source_encoding in source_encodings:
                try:
                    raw = item.encode(source_encoding)
                except (LookupError, UnicodeEncodeError):
                    continue
                for target_encoding in target_encodings:
                    try:
                        candidate = raw.decode(target_encoding)
                    except (LookupError, UnicodeDecodeError):
                        continue
                    if candidate and candidate not in candidates:
                        candidates.add(candidate)
                        next_frontier.add(candidate)
        if not next_frontier:
            break
        frontier = next_frontier

    # 第二组保留旧逻辑，用于少量“先按 GBK/Big5/UTF-16 错转，再应还原为 UTF-8”的边界样本。
    for source_encoding in ("utf-16le", "gb18030", "gbk", "big5"):
        try:
            candidates.add(text.encode(source_encoding).decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue

    best_text = max(candidates, key=score_decoded_text)
    best_score = score_decoded_text(best_text)
    if strong_mojibake_signal and best_score > original_score:
        return best_text
    if best_score > original_score + 10:
        return best_text
    return text


def has_latin1_mojibake_signal(text: str) -> bool:
    """判断文本是否像 UTF-8 字节被 Latin1/CP1252 错解后的结果。"""
    if any("\x80" <= char <= "\x9f" for char in text):
        return True
    return bool(re.search(r"[ÃÂÄÅÆÇÈÉ][\x80-\xff]", text)) or bool(re.search(r"(?:Ã.|Â.|Ä.|Å.){2,}", text))


def mojibake_score(text: str) -> int:
    markers = (
        "锟", "閿", "�", "Ã", "Â", "¤", "¥", "¢", "ä", "å", "æ", "ç", "è", "é", "½", "¡",
        "浣", "犲", "ソ", "瑕", "侀", "€", "锛", "冿", "紝",
        "鏂", "鎬", "煡", "鐪", "閫", "氱", "洿", "规", "嵁", "涓", "湪",
        "娆", "瀹", "㈡", "湇", "绛", "伐", "佷", "叆", "妯", "婊", "卞", "勭",
        "瑞", "偶", "橱", "嫡", "皴", "仟", "软", "钧", "绮", "谩", "陲",
    )
    marker_score = sum(text.count(marker) for marker in markers)
    latin1_run_score = len(re.findall(r"[ÃÂ][\x80-\xffA-Za-z]{1,}", text))
    utf8_as_latin1_score = len(re.findall(r"[äåæçèé][\x80-\xffA-Za-z]{1,}", text))
    cjk_mojibake_run_score = len(re.findall(r"[浣犲ソ鏂鎬煡鐪嬫湁涓侀氱洿规嵁瀹㈡湇]{2,}", text))
    return marker_score + latin1_run_score * 3 + utf8_as_latin1_score * 3 + cjk_mojibake_run_score * 2


COMMON_CHINESE_CHARS = set("的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处队南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联什认六共权收证改清美再采转更单风切打白教速花带安场身车例真务具万每目至达走积示议声报斗完类八离华名确才科张信马节话米整空元况今集温传土许步群广石记需段研界拉林律叫且究观越织装影算低持音众书布复容儿须际商非验连断深难近矿千周委素技备半办青省列习响约支般史感劳便团往酸历市克何除消构府称太准精值号率族维划选标写存候毛亲快效斯院查江型眼王按格养易置派层片始却专状育厂京识适属圆包火住调满县局照参红细引听该铁价严龙飞")
COMMON_ENGLISH_WORDS = {
    "the", "and", "for", "you", "your", "this", "that", "with", "from", "please", "thanks",
    "account", "email", "support", "security", "login", "subscription", "refund", "invoice",
    "team", "customer", "service", "plan", "access", "verify", "notification", "update",
}


def utf16_ascii_pair_score(text: str) -> int:
    suspicious = 0
    for char in text:
        if "\u4e00" <= char <= "\u9fff":
            continue
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
    # UTF-8 放在候选里而不是提前返回，避免“错误但能解码”的文本跳过质量评分。
    candidates.extend(["utf-8", "utf-8-sig", "gb18030", "gbk", "gb2312", "big5", "utf-16", "utf-16le", "utf-16be", "latin-1", "cp1252"])
    return list(dict.fromkeys(candidates))


def score_decoded_text(text: str) -> int:
    if not text:
        return -100
    replacement_penalty = text.count("�") * 20 + text.count("\x00") * 10
    question_run_penalty = text.count("????") * 10
    utf16_pair_penalty = utf16_ascii_pair_score(text) * 80
    mojibake_penalty = mojibake_score(text) * 35
    latin1_mojibake_penalty = len(re.findall(r"(?:Ã.|Â.|Ä.|Å.|ä.|å.|æ.|ç.|è.|é.){2,}", text)) * 45
    latin1_byte_penalty = 0
    latin1_extended_count = sum(1 for char in text if 0x80 <= ord(char) <= 0xFF)
    if latin1_extended_count >= 4:
        latin1_byte_penalty = latin1_extended_count * 12
    hangul_count = sum(1 for char in text if "\uac00" <= char <= "\ud7af")
    private_use_count = sum(1 for char in text if "\ue000" <= char <= "\uf8ff")
    cjk_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    common_chinese_count = sum(1 for char in text if char in COMMON_CHINESE_CHARS)
    # 错误编码经常会产生大量“看似中文但不成句”的罕见汉字。真实中文邮件里，
    # “的、一、是、我、你、请”等常用字比例通常不会太低。
    random_cjk_penalty = 0
    if cjk_count >= 20 and common_chinese_count / max(cjk_count, 1) < 0.08:
        random_cjk_penalty = cjk_count * 4
    chinese_bonus = common_chinese_count * 4
    readable_bonus = sum(1 for char in text if char.isprintable() or char in "\r\n\t")
    ascii_bonus = sum(1 for char in text if char.isascii() and (char.isalnum() or char in " .,;:/-_@<>")) * 2
    words = re.findall(r"[A-Za-z]{2,}", text.lower())
    english_bonus = sum(6 for word in words if word in COMMON_ENGLISH_WORDS)
    url_email_bonus = len(re.findall(r"https?://|[\w.+-]+@[\w.-]+", text)) * 12
    return (
        readable_bonus
        + chinese_bonus
        + ascii_bonus
        + english_bonus
        + url_email_bonus
        - replacement_penalty
        - question_run_penalty
        - utf16_pair_penalty
        - mojibake_penalty
        - latin1_mojibake_penalty
        - random_cjk_penalty
        - latin1_byte_penalty
        - hangul_count * 12
        - private_use_count * 20
    )


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
