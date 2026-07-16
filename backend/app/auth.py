"""后台登录认证与 RBAC 权限控制。

这个模块刻意不引入额外 JWT / 密码库，而是使用 Python 标准库实现：
- PBKDF2-HMAC-SHA256：用于密码哈希，避免保存明文密码。
- HMAC-SHA256 JWT：用于前端登录后的接口认证。

这样项目部署更轻，也方便在面试中清楚解释认证链路。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.db import SessionLocal, init_db
from app.db_models import UserORM

UserRole = Literal["admin", "manager", "agent"]

JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-change-me")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))
PASSWORD_ITERATIONS = 120_000
security = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=6, max_length=128)


class UserProfile(BaseModel):
    id: str
    username: str
    display_name: str
    role: UserRole


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserProfile


def ensure_default_users() -> None:
    """初始化演示环境默认账号。

    真实企业系统会接入统一身份源或由管理员创建账号。当前项目为了本地演示，
    在空用户表时创建三个角色账号，方便直接验证 RBAC 效果。
    """
    init_db()
    with SessionLocal() as session:
        user_count = session.scalar(select(func.count()).select_from(UserORM))
        if user_count:
            return
        default_password = os.getenv("DEFAULT_ADMIN_PASSWORD", "Admin123456")
        users = [
            UserORM(username="admin", display_name="系统管理员", role="admin", password_hash=hash_password(default_password)),
            UserORM(username="manager", display_name="客服主管", role="manager", password_hash=hash_password(default_password)),
            UserORM(username="agent", display_name="客服人员", role="agent", password_hash=hash_password(default_password)),
        ]
        session.add_all(users)
        session.commit()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        expected = base64.urlsafe_b64decode(digest_b64.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def authenticate_user(username: str, password: str) -> UserORM | None:
    ensure_default_users()
    with SessionLocal() as session:
        user = session.scalar(select(UserORM).where(UserORM.username == username))
        if not user or not user.is_active or not verify_password(password, user.password_hash):
            return None
        user.last_login_at = datetime.utcnow()
        session.commit()
        session.refresh(user)
        return user


def create_access_token(user: UserORM) -> str:
    expire_at = datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "exp": int(expire_at.timestamp()),
    }
    return encode_jwt(payload)


def encode_jwt(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{base64url_json(header)}.{base64url_json(payload)}"
    signature = hmac.new(JWT_SECRET.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
    return f"{signing_input}.{base64url_encode(signature)}"


def decode_jwt(token: str) -> dict:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".", 2)
        signing_input = f"{header_b64}.{payload_b64}"
        expected = hmac.new(JWT_SECRET.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
        if not hmac.compare_digest(base64url_encode(expected), signature_b64):
            raise ValueError("bad signature")
        payload = json.loads(base64url_decode(payload_b64).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(datetime.utcnow().timestamp()):
            raise ValueError("expired")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc


def base64url_json(value: dict) -> str:
    return base64url_encode(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("utf-8"))


def to_profile(user: UserORM) -> UserProfile:
    return UserProfile(id=user.id, username=user.username, display_name=user.display_name or user.username, role=user.role)  # type: ignore[arg-type]


def get_current_user(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)]) -> UserORM:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    payload = decode_jwt(credentials.credentials)
    ensure_default_users()
    with SessionLocal() as session:
        user = session.get(UserORM, payload.get("sub"))
        if not user or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User is disabled or missing")
        session.expunge(user)
        return user


CurrentUser = Annotated[UserORM, Depends(get_current_user)]


def require_roles(roles: list[UserRole]):
    """生成 FastAPI 权限依赖。

    用法：``current_user: UserORM = Depends(require_roles(["admin", "manager"]))``。
    """

    def checker(current_user: CurrentUser) -> UserORM:
        if current_user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied")
        return current_user

    return checker
