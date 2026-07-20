"""统一邮件源配置、连接测试、收信与发信服务。

QQ 使用标准 IMAP/SMTP；Gmail 与 Outlook 可在 IMAP/SMTP 和 API OAuth 间切换。
连接配置只有在测试成功后才会成为活动配置，密钥字段使用 Fernet 加密落库，API
只返回“是否已配置”，不会把明文或密文返回给浏览器。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from datetime import datetime
from email.message import Message
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote, urlencode

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
    parse_raw_email,
    send_smtp_email,
    test_imap_smtp_connection,
)
from app.models import EmailAttachment, EmailCreate, MailAuthMode, MailProvider, MailSourceConfigInput, MailSourceInfo, MailSourceState

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

PROVIDER_LABELS: dict[str, str] = {"qq": "QQ 邮箱", "outlook": "Outlook", "gmail": "Gmail"}
SECRET_FIELDS = {"credential", "client_secret", "refresh_token"}
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
OUTLOOK_SCOPES = [
    "offline_access",
    "https://graph.microsoft.com/User.Read",
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.Send",
]


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
            "auth_mode": "imap_smtp",
            "email_address": os.getenv("QQ_EMAIL_ADDRESS", ""),
            "credential": os.getenv("QQ_EMAIL_AUTH_CODE", ""),
            "imap_host": os.getenv("QQ_IMAP_HOST", "imap.qq.com"),
            "imap_port": int(os.getenv("QQ_IMAP_PORT", "993")),
            "smtp_host": os.getenv("QQ_SMTP_HOST", "smtp.qq.com"),
            "smtp_port": int(os.getenv("QQ_SMTP_PORT", "465")),
        }
    if provider == "gmail":
        return {
            "auth_mode": os.getenv("GMAIL_AUTH_MODE", "imap_smtp"),
            "email_address": os.getenv("GMAIL_EMAIL_ADDRESS", ""),
            "credential": os.getenv("GMAIL_APP_PASSWORD", ""),
            "imap_host": os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com"),
            "imap_port": int(os.getenv("GMAIL_IMAP_PORT", "993")),
            "smtp_host": os.getenv("GMAIL_SMTP_HOST", "smtp.gmail.com"),
            "smtp_port": int(os.getenv("GMAIL_SMTP_PORT", "465")),
            **_gmail_client_config(),
        }
    return {
        "auth_mode": os.getenv("OUTLOOK_AUTH_MODE", "api_oauth"),
        "email_address": os.getenv("OUTLOOK_EMAIL_ADDRESS", ""),
        "credential": os.getenv("OUTLOOK_APP_PASSWORD", ""),
        "imap_host": os.getenv("OUTLOOK_IMAP_HOST", "outlook.office365.com"),
        "imap_port": int(os.getenv("OUTLOOK_IMAP_PORT", "993")),
        "smtp_host": os.getenv("OUTLOOK_SMTP_HOST", "smtp-mail.outlook.com"),
        "smtp_port": int(os.getenv("OUTLOOK_SMTP_PORT", "587")),
        "tenant_id": os.getenv("OUTLOOK_TENANT_ID", "consumers"),
        "client_id": os.getenv("OUTLOOK_CLIENT_ID", ""),
        "client_secret": os.getenv("OUTLOOK_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("OUTLOOK_REDIRECT_URI", ""),
    }


def _gmail_client_config() -> dict[str, object]:
    """读取 Google Web OAuth 客户端，不把 Client Secret 返回到前端。"""
    result: dict[str, object] = {
        "client_id": os.getenv("GMAIL_CLIENT_ID", ""),
        "client_secret": os.getenv("GMAIL_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("GMAIL_REDIRECT_URI", ""),
    }
    credentials_path = Path(
        os.getenv(
            "GMAIL_OAUTH_CREDENTIALS_FILE",
            str(Path(__file__).resolve().parents[1] / "secrets" / "gmail_credentials.json"),
        )
    )
    if not credentials_path.exists():
        return result
    try:
        payload = json.loads(credentials_path.read_text(encoding="utf-8"))
        node = payload.get("web") or payload.get("installed") or {}
    except (OSError, json.JSONDecodeError):
        return result
    redirect_uris = node.get("redirect_uris") or []
    return {
        "client_id": node.get("client_id") or result["client_id"],
        "client_secret": node.get("client_secret") or result["client_secret"],
        "redirect_uri": (redirect_uris[0] if redirect_uris else "") or result["redirect_uri"],
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


def _auth_mode(provider: MailProvider, config: dict[str, object]) -> MailAuthMode:
    value = str(config.get("auth_mode", "")).strip()
    if value in {"imap_smtp", "api_oauth"}:
        return value  # type: ignore[return-value]
    return "api_oauth" if provider == "outlook" else "imap_smtp"


def get_active_provider() -> MailProvider:
    with SessionLocal() as session:
        row = session.scalar(select(MailSourceConfigORM).where(MailSourceConfigORM.is_active.is_(True)))
        if row and row.provider in PROVIDER_LABELS:
            return row.provider  # type: ignore[return-value]
    provider = os.getenv("MAIL_PROVIDER", "qq").strip().lower()
    return provider if provider in PROVIDER_LABELS else "qq"  # type: ignore[return-value]


def _is_configured(provider: MailProvider, config: dict[str, object]) -> bool:
    mode = _auth_mode(provider, config)
    if mode == "api_oauth":
        if provider == "qq":
            return False
        required = ("email_address", "client_id", "client_secret", "redirect_uri", "refresh_token")
    else:
        required = ("email_address", "credential", "imap_host", "imap_port", "smtp_host", "smtp_port")
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
                auth_mode=_auth_mode(typed_provider, config),
                label=PROVIDER_LABELS[provider],
                active=provider == active,
                configured=_is_configured(typed_provider, config),
                secret_configured=bool(config.get("credential") or config.get("client_secret") or config.get("refresh_token")),
                imap_secret_configured=bool(config.get("credential")),
                oauth_secret_configured=bool(config.get("client_secret")),
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
    if _auth_mode(payload.provider, config) == "api_oauth":
        raise MailClientConfigError("API OAuth 需要先完成浏览器授权，请点击“前往授权”")
    if not _is_configured(payload.provider, config):
        raise MailClientConfigError("配置不完整，请填写邮箱地址和该邮件源要求的全部认证信息")
    test_provider_connection(payload.provider, config)

    _persist_provider_config(payload.provider, config, activate=True)
    return get_mail_source_state()


def _persist_provider_config(provider: MailProvider, config: dict[str, object], *, activate: bool) -> None:
    plain = {key: value for key, value in config.items() if key not in SECRET_FIELDS}
    secrets = {key: str(value) for key, value in config.items() if key in SECRET_FIELDS and value}
    with SessionLocal() as session:
        if activate:
            session.execute(update(MailSourceConfigORM).values(is_active=False))
        row = session.get(MailSourceConfigORM, provider)
        if not row:
            row = MailSourceConfigORM(provider=provider)
            session.add(row)
        row.config = plain
        row.encrypted_secrets = _encrypt_secrets(secrets)
        row.is_active = activate
        row.updated_at = datetime.utcnow()
        session.commit()


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


def begin_oauth(payload: MailSourceConfigInput) -> str:
    """生成服务端 OAuth 授权地址；配置被加密放入短时 state，不提前切换邮件源。"""
    if payload.provider == "qq" or payload.auth_mode != "api_oauth":
        raise MailClientConfigError("该邮件源不支持 API OAuth 授权")
    config = _merge_input(payload)
    required = ("email_address", "client_id", "client_secret", "redirect_uri")
    if not all(config.get(field) not in (None, "") for field in required):
        raise MailClientConfigError("OAuth 配置不完整，请填写邮箱、Client ID、Client Secret 和回调地址")
    state_payload = {
        "provider": payload.provider,
        "issued_at": int(time.time()),
        "config": config,
    }
    state = _fernet().encrypt(json.dumps(state_payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    if payload.provider == "gmail":
        params = {
            "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"],
            "response_type": "code",
            "scope": " ".join(GMAIL_SCOPES),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "login_hint": config["email_address"],
            "state": state,
        }
        return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    tenant = quote(str(config.get("tenant_id") or "consumers"), safe="")
    params = {
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
        "response_type": "code",
        "response_mode": "query",
        "scope": " ".join(OUTLOOK_SCOPES),
        "login_hint": config["email_address"],
        "state": state,
    }
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize?{urlencode(params)}"


def complete_oauth(provider: MailProvider, *, code: str, state: str) -> None:
    """校验回调 state、换取 Refresh Token，并在连通性验证后激活邮件源。"""
    try:
        payload = json.loads(_fernet().decrypt(state.encode("ascii"), ttl=600).decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise MailClientConfigError("OAuth 授权状态已失效，请回到系统设置重新发起授权") from exc
    if payload.get("provider") != provider:
        raise MailClientConfigError("OAuth 回调服务商与授权请求不一致")
    config = dict(payload.get("config") or {})
    token_payload = _exchange_authorization_code(provider, config, code)
    refresh_token = token_payload.get("refresh_token")
    if not refresh_token:
        raise MailClientConfigError("授权成功但未获得 Refresh Token，请撤销应用授权后重新同意")
    config.update({"auth_mode": "api_oauth", "refresh_token": refresh_token})
    access_token = str(token_payload.get("access_token", ""))
    if provider == "gmail":
        _api_request(provider, config, "GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile", access_token=access_token)
    else:
        _api_request(provider, config, "GET", "https://graph.microsoft.com/v1.0/me?$select=id,mail,userPrincipalName", access_token=access_token)
    _persist_provider_config(provider, config, activate=True)


def _exchange_authorization_code(provider: MailProvider, config: dict[str, object], code: str) -> dict:
    if provider == "gmail":
        url = "https://oauth2.googleapis.com/token"
        scope = None
    else:
        tenant = quote(str(config.get("tenant_id") or "consumers"), safe="")
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        scope = " ".join(OUTLOOK_SCOPES)
    data = {
        "client_id": str(config["client_id"]),
        "client_secret": str(config["client_secret"]),
        "redirect_uri": str(config["redirect_uri"]),
        "code": code,
        "grant_type": "authorization_code",
    }
    if scope:
        data["scope"] = scope
    return _token_request(url, data, provider)


def _oauth_access_token(provider: MailProvider, config: dict[str, object]) -> str:
    if provider == "gmail":
        url = "https://oauth2.googleapis.com/token"
        scope = None
    else:
        tenant = quote(str(config.get("tenant_id") or "consumers"), safe="")
        url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        scope = " ".join(OUTLOOK_SCOPES)
    data = {
        "client_id": str(config["client_id"]),
        "client_secret": str(config["client_secret"]),
        "refresh_token": str(config["refresh_token"]),
        "grant_type": "refresh_token",
    }
    if scope:
        data["scope"] = scope
    return str(_token_request(url, data, provider)["access_token"])


def _token_request(url: str, data: dict[str, str], provider: MailProvider) -> dict:
    try:
        response = httpx.post(url, data=data, timeout=20)
    except httpx.HTTPError as exc:
        raise MailClientConfigError(f"无法连接 {PROVIDER_LABELS[provider]} 授权服务") from exc
    if not response.is_success:
        try:
            detail = response.json().get("error_description") or response.json().get("error") or response.text
        except ValueError:
            detail = response.text
        raise MailClientConfigError(f"{PROVIDER_LABELS[provider]} 授权失败：{str(detail)[:500]}")
    return response.json()


def _api_request(provider: MailProvider, config: dict[str, object], method: str, url: str, *, access_token: str = "", **kwargs) -> httpx.Response:
    token = access_token or _oauth_access_token(provider, config)
    try:
        response = httpx.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise MailClientConfigError(f"无法连接 {PROVIDER_LABELS[provider]} API") from exc
    if not response.is_success:
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            detail = response.text
        raise MailClientConfigError(f"{PROVIDER_LABELS[provider]} API 请求失败：{str(detail)[:500]}")
    return response


def test_provider_connection(provider: MailProvider, config: dict[str, object]) -> None:
    if _auth_mode(provider, config) == "imap_smtp":
        try:
            test_imap_smtp_connection(_imap_settings(provider, config))
        except MailClientConfigError:
            raise
        except Exception as exc:
            raise MailClientConfigError(f"{PROVIDER_LABELS[provider]}连接验证失败，请检查邮箱地址、授权码和服务器配置") from exc
        return
    if provider == "gmail":
        _api_request(provider, config, "GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile")
    else:
        _api_request(provider, config, "GET", "https://graph.microsoft.com/v1.0/me/mailFolders/inbox?$select=id,displayName")


def fetch_active_emails(limit: int, known_message_ids: set[str]) -> tuple[MailProvider, list[ImportedMail]]:
    provider = get_active_provider()
    config = get_provider_config(provider)
    if not _is_configured(provider, config):
        raise MailClientConfigError(f"{PROVIDER_LABELS[provider]} 尚未完成配置")
    if _auth_mode(provider, config) == "imap_smtp":
        try:
            imported = fetch_imap_emails(_imap_settings(provider, config), limit=limit, known_message_ids=known_message_ids)
        except Exception as exc:
            raise MailClientConfigError(f"{PROVIDER_LABELS[provider]}同步失败，请检查网络和邮件源配置") from exc
        return provider, imported
    if provider == "gmail":
        return provider, _fetch_gmail_emails(config, limit, known_message_ids)
    return provider, _fetch_outlook_emails(config, limit, known_message_ids)


def _fetch_gmail_emails(config: dict[str, object], limit: int, known_ids: set[str]) -> list[ImportedMail]:
    params = {"maxResults": min(max(limit, 1), 50), "q": "in:inbox"}
    values = _api_request("gmail", config, "GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages", params=params).json().get("messages", [])
    imported: list[ImportedMail] = []
    for item in values:
        message_id = str(item.get("id", ""))
        stable_id = f"gmail:{message_id}"
        if not message_id or stable_id in known_ids:
            continue
        raw = _api_request("gmail", config, "GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{quote(message_id, safe='')}?format=raw").json().get("raw", "")
        if not raw:
            continue
        padded = raw + "=" * (-len(raw) % 4)
        imported.append(parse_raw_email(base64.urlsafe_b64decode(padded), provider="gmail", provider_message_id=stable_id))
    return imported


def _fetch_outlook_emails(config: dict[str, object], limit: int, known_ids: set[str]) -> list[ImportedMail]:
    params = {
        "$top": min(max(limit, 1), 50),
        "$orderby": "receivedDateTime desc",
        "$select": "id,internetMessageId,subject,from,body,hasAttachments,receivedDateTime",
    }
    values = _api_request("outlook", config, "GET", "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages", params=params).json().get("value", [])
    imported: list[ImportedMail] = []
    for item in values:
        message_id = item.get("internetMessageId") or item.get("id", "")
        if not message_id or message_id in known_ids:
            continue
        sender = item.get("from", {}).get("emailAddress", {})
        body = clean_email_text(item.get("body", {}).get("content", "")) or "(empty email body)"
        attachments = _fetch_outlook_attachments(config, item["id"]) if item.get("hasAttachments") else []
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


def _fetch_outlook_attachments(config: dict[str, object], message_id: str) -> list[EmailAttachment]:
    values = _api_request("outlook", config, "GET", f"https://graph.microsoft.com/v1.0/me/messages/{quote(message_id, safe='')}/attachments").json().get("value", [])
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
    if _auth_mode(provider, config) == "imap_smtp":
        try:
            send_smtp_email(_imap_settings(provider, config), to_address=to_address, subject=subject, body=body)
        except Exception as exc:
            raise MailClientConfigError(f"{PROVIDER_LABELS[provider]}发送失败，请检查 SMTP 配置") from exc
    elif provider == "gmail":
        message = MIMEText(body, "plain", "utf-8")
        message["From"] = str(config["email_address"])
        message["To"] = to_address
        message["Subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        _api_request("gmail", config, "POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", json={"raw": raw})
    else:
        _api_request(
            "outlook",
            config,
            "POST",
            "https://graph.microsoft.com/v1.0/me/sendMail",
            json={"message": {"subject": subject, "body": {"contentType": "Text", "content": body}, "toRecipients": [{"emailAddress": {"address": to_address}}]}, "saveToSentItems": True},
        )
    return provider
