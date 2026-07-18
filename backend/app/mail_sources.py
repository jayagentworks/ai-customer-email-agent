"""统一邮件源配置、连接测试、收信与发信服务。

QQ 与 Gmail 使用标准 IMAP/SMTP；Outlook 使用 Microsoft Graph 的应用权限模式。
连接配置只有在测试成功后才会成为活动配置，密钥字段使用 Fernet 加密落库，API
只返回“是否已配置”，不会把明文或密文返回给浏览器。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime
from email.message import Message
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from sqlalchemy import select, update

from app.db import SessionLocal
from app.db_models import MailSourceConfigORM
from app.mail_client import (
    ImapSmtpSettings,
    ImportedMail,
    MailClientConfigError,
    clean_email_text,
    fetch_imap_emails,
    parse_attachment_payload,
    send_smtp_email,
    test_imap_smtp_connection,
)
from app.models import EmailAttachment, EmailCreate, MailProvider, MailSourceConfigInput, MailSourceInfo, MailSourceState

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

PROVIDER_LABELS: dict[str, str] = {"qq": "QQ 邮箱", "outlook": "Outlook", "gmail": "Gmail"}
SECRET_FIELDS = {"credential", "client_secret"}


def _fernet() -> Fernet:
    configured = os.getenv("MAIL_CONFIG_ENCRYPTION_KEY", "").strip()
    if configured:
        try:
            return Fernet(configured.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise MailClientConfigError("MAIL_CONFIG_ENCRYPTION_KEY 不是有效的 Fernet 密钥") from exc
    # 本地开发环境可由 JWT 密钥稳定派生；生产环境应单独配置 MAIL_CONFIG_ENCRYPTION_KEY。
    seed = os.getenv("JWT_SECRET", "dev-only-change-me").encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))


def _encrypt_secrets(values: dict[str, str]) -> str:
    payload = json.dumps(values, ensure_ascii=False).encode("utf-8")
    return _fernet().encrypt(payload).decode("ascii")


def _decrypt_secrets(value: str) -> dict[str, str]:
    if not value:
        return {}
    try:
        return json.loads(_fernet().decrypt(value.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise MailClientConfigError("已保存的邮件源密钥无法解密，请管理员重新配置") from exc


def _environment_config(provider: MailProvider) -> dict[str, object]:
    if provider == "qq":
        return {
            "email_address": os.getenv("QQ_EMAIL_ADDRESS", ""),
            "credential": os.getenv("QQ_EMAIL_AUTH_CODE", ""),
            "imap_host": os.getenv("QQ_IMAP_HOST", "imap.qq.com"),
            "imap_port": int(os.getenv("QQ_IMAP_PORT", "993")),
            "smtp_host": os.getenv("QQ_SMTP_HOST", "smtp.qq.com"),
            "smtp_port": int(os.getenv("QQ_SMTP_PORT", "465")),
        }
    if provider == "gmail":
        return {
            "email_address": os.getenv("GMAIL_EMAIL_ADDRESS", ""),
            "credential": os.getenv("GMAIL_APP_PASSWORD", ""),
            "imap_host": os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com"),
            "imap_port": int(os.getenv("GMAIL_IMAP_PORT", "993")),
            "smtp_host": os.getenv("GMAIL_SMTP_HOST", "smtp.gmail.com"),
            "smtp_port": int(os.getenv("GMAIL_SMTP_PORT", "465")),
        }
    return {
        "email_address": os.getenv("OUTLOOK_EMAIL_ADDRESS", ""),
        "tenant_id": os.getenv("OUTLOOK_TENANT_ID", ""),
        "client_id": os.getenv("OUTLOOK_CLIENT_ID", ""),
        "client_secret": os.getenv("OUTLOOK_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("OUTLOOK_REDIRECT_URI", ""),
    }


def _stored_config(provider: MailProvider) -> dict[str, object]:
    with SessionLocal() as session:
        row = session.get(MailSourceConfigORM, provider)
        if not row:
            return {}
        return {**(row.config or {}), **_decrypt_secrets(row.encrypted_secrets)}


def get_provider_config(provider: MailProvider) -> dict[str, object]:
    """返回服务端完整配置；数据库配置优先，空字段由环境变量兜底。"""
    env = _environment_config(provider)
    stored = _stored_config(provider)
    return {key: value for key, value in {**env, **stored}.items() if value not in (None, "")}


def get_active_provider() -> MailProvider:
    with SessionLocal() as session:
        row = session.scalar(select(MailSourceConfigORM).where(MailSourceConfigORM.is_active.is_(True)))
        if row and row.provider in PROVIDER_LABELS:
            return row.provider  # type: ignore[return-value]
    provider = os.getenv("MAIL_PROVIDER", "qq").strip().lower()
    return provider if provider in PROVIDER_LABELS else "qq"  # type: ignore[return-value]


def _is_configured(provider: MailProvider, config: dict[str, object]) -> bool:
    required = (
        ("email_address", "credential", "imap_host", "imap_port", "smtp_host", "smtp_port")
        if provider in {"qq", "gmail"}
        else ("email_address", "tenant_id", "client_id", "client_secret")
    )
    return all(config.get(field) not in (None, "") for field in required)


def get_mail_source_state() -> MailSourceState:
    active = get_active_provider()
    sources: list[MailSourceInfo] = []
    for provider in ("qq", "outlook", "gmail"):
        typed_provider: MailProvider = provider  # type: ignore[assignment]
        config = get_provider_config(typed_provider)
        sources.append(
            MailSourceInfo(
                provider=typed_provider,
                label=PROVIDER_LABELS[provider],
                active=provider == active,
                configured=_is_configured(typed_provider, config),
                secret_configured=bool(config.get("credential") or config.get("client_secret")),
                email_address=str(config.get("email_address", "")),
                imap_host=str(config.get("imap_host", "")),
                imap_port=int(config["imap_port"]) if config.get("imap_port") else None,
                smtp_host=str(config.get("smtp_host", "")),
                smtp_port=int(config["smtp_port"]) if config.get("smtp_port") else None,
                tenant_id=str(config.get("tenant_id", "")),
                client_id=str(config.get("client_id", "")),
                redirect_uri=str(config.get("redirect_uri", "")),
            )
        )
    return MailSourceState(active_provider=active, sources=sources)


def _merge_input(payload: MailSourceConfigInput) -> dict[str, object]:
    current = get_provider_config(payload.provider)
    submitted = payload.model_dump(exclude={"provider"})
    # 密钥留空表示沿用服务端已有值，普通字段留空也沿用默认主机等现有设置。
    return {**current, **{key: value for key, value in submitted.items() if value not in (None, "")}}


def configure_and_activate(payload: MailSourceConfigInput) -> MailSourceState:
    config = _merge_input(payload)
    if not _is_configured(payload.provider, config):
        raise MailClientConfigError("配置不完整，请填写邮箱地址和该邮件源要求的全部认证信息")
    test_provider_connection(payload.provider, config)

    plain = {key: value for key, value in config.items() if key not in SECRET_FIELDS}
    secrets = {key: str(value) for key, value in config.items() if key in SECRET_FIELDS and value}
    with SessionLocal() as session:
        session.execute(update(MailSourceConfigORM).values(is_active=False))
        row = session.get(MailSourceConfigORM, payload.provider)
        if not row:
            row = MailSourceConfigORM(provider=payload.provider)
            session.add(row)
        row.config = plain
        row.encrypted_secrets = _encrypt_secrets(secrets)
        row.is_active = True
        row.updated_at = datetime.utcnow()
        session.commit()
    return get_mail_source_state()


def _imap_settings(provider: MailProvider, config: dict[str, object]) -> ImapSmtpSettings:
    return ImapSmtpSettings(
        provider=provider,
        email_address=str(config["email_address"]),
        credential=str(config["credential"]),
        imap_host=str(config["imap_host"]),
        imap_port=int(config["imap_port"]),
        smtp_host=str(config["smtp_host"]),
        smtp_port=int(config["smtp_port"]),
    )


def _graph_token(config: dict[str, object]) -> str:
    url = f"https://login.microsoftonline.com/{quote(str(config['tenant_id']), safe='')}/oauth2/v2.0/token"
    try:
        response = httpx.post(
            url,
            data={
                "client_id": str(config["client_id"]),
                "client_secret": str(config["client_secret"]),
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise MailClientConfigError("无法连接 Microsoft 登录服务") from exc
    if not response.is_success:
        detail = response.json().get("error_description", response.text)[:500]
        raise MailClientConfigError(f"Outlook 授权失败：{detail}")
    return str(response.json()["access_token"])


def _graph_request(config: dict[str, object], method: str, path: str, **kwargs) -> httpx.Response:
    token = _graph_token(config)
    try:
        response = httpx.request(
            method,
            f"https://graph.microsoft.com/v1.0{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise MailClientConfigError("无法连接 Microsoft Graph") from exc
    if not response.is_success:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            detail = response.text
        raise MailClientConfigError(f"Outlook Graph 请求失败：{str(detail)[:500]}")
    return response


def test_provider_connection(provider: MailProvider, config: dict[str, object]) -> None:
    if provider in {"qq", "gmail"}:
        try:
            test_imap_smtp_connection(_imap_settings(provider, config))
        except MailClientConfigError:
            raise
        except Exception as exc:
            raise MailClientConfigError(f"{PROVIDER_LABELS[provider]}连接验证失败，请检查邮箱地址、授权码和服务器配置") from exc
        return
    mailbox = quote(str(config["email_address"]), safe="")
    _graph_request(config, "GET", f"/users/{mailbox}/mailFolders/inbox?$select=id,displayName")


def fetch_active_emails(limit: int, known_message_ids: set[str]) -> tuple[MailProvider, list[ImportedMail]]:
    provider = get_active_provider()
    config = get_provider_config(provider)
    if not _is_configured(provider, config):
        raise MailClientConfigError(f"{PROVIDER_LABELS[provider]} 尚未完成配置")
    if provider in {"qq", "gmail"}:
        try:
            imported = fetch_imap_emails(_imap_settings(provider, config), limit=limit, known_message_ids=known_message_ids)
        except Exception as exc:
            raise MailClientConfigError(f"{PROVIDER_LABELS[provider]}同步失败，请检查网络和邮件源配置") from exc
        return provider, imported
    return provider, _fetch_outlook_emails(config, limit, known_message_ids)


def _fetch_outlook_emails(config: dict[str, object], limit: int, known_ids: set[str]) -> list[ImportedMail]:
    mailbox = quote(str(config["email_address"]), safe="")
    params = {
        "$top": min(max(limit, 1), 50),
        "$orderby": "receivedDateTime desc",
        "$select": "id,internetMessageId,subject,from,body,hasAttachments,receivedDateTime",
    }
    values = _graph_request(config, "GET", f"/users/{mailbox}/mailFolders/inbox/messages", params=params).json().get("value", [])
    imported: list[ImportedMail] = []
    for item in values:
        message_id = item.get("internetMessageId") or item.get("id", "")
        if not message_id or message_id in known_ids:
            continue
        sender = item.get("from", {}).get("emailAddress", {})
        body = clean_email_text(item.get("body", {}).get("content", "")) or "(empty email body)"
        attachments = _fetch_outlook_attachments(config, mailbox, item["id"]) if item.get("hasAttachments") else []
        imported.append(
            ImportedMail(
                payload=EmailCreate(
                    customer_name=sender.get("name") or sender.get("address") or "Outlook Mail User",
                    customer_email=sender.get("address", ""),
                    subject=item.get("subject") or "(no subject)",
                    body=body,
                    attachments=attachments,
                ),
                provider_message_id=message_id,
            )
        )
    return imported


def _fetch_outlook_attachments(config: dict[str, object], mailbox: str, message_id: str) -> list[EmailAttachment]:
    values = _graph_request(config, "GET", f"/users/{mailbox}/messages/{quote(message_id, safe='')}/attachments").json().get("value", [])
    attachments: list[EmailAttachment] = []
    for item in values:
        name = item.get("name") or "attachment"
        content_type = item.get("contentType") or "application/octet-stream"
        encoded = item.get("contentBytes")
        if not encoded:
            attachments.append(EmailAttachment(filename=name, content_type=content_type, size_bytes=int(item.get("size", 0)), parse_status="metadata_only"))
            continue
        try:
            payload = base64.b64decode(encoded)
            part = Message()
            part["Content-Disposition"] = f'attachment; filename="{name}"'
            preview, status, message, report = parse_attachment_payload(name, content_type, payload, part)
            attachments.append(EmailAttachment(filename=name, content_type=content_type, size_bytes=len(payload), text_preview=preview, parse_status=status, status_message=message, parse_report=report))
        except Exception:
            attachments.append(EmailAttachment(filename=name, content_type=content_type, size_bytes=int(item.get("size", 0)), parse_status="failed", status_message="附件解析失败"))
    return attachments


def send_active_email(*, to_address: str, subject: str, body: str) -> MailProvider:
    provider = get_active_provider()
    config = get_provider_config(provider)
    if not _is_configured(provider, config):
        raise MailClientConfigError(f"{PROVIDER_LABELS[provider]} 尚未完成配置")
    if provider in {"qq", "gmail"}:
        try:
            send_smtp_email(_imap_settings(provider, config), to_address=to_address, subject=subject, body=body)
        except Exception as exc:
            raise MailClientConfigError(f"{PROVIDER_LABELS[provider]}发送失败，请检查 SMTP 配置") from exc
    else:
        mailbox = quote(str(config["email_address"]), safe="")
        _graph_request(
            config,
            "POST",
            f"/users/{mailbox}/sendMail",
            json={"message": {"subject": subject, "body": {"contentType": "Text", "content": body}, "toRecipients": [{"emailAddress": {"address": to_address}}]}, "saveToSentItems": True},
        )
    return provider
