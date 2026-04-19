import hashlib
import time
from typing import Optional
import jwt
from fastapi import Request, HTTPException

SALT = "scs_salt_2026"
JWT_SECRET = "scs_jwt_secret_bupt_2026"
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_SECONDS = 86400  # 24 小时


def hash_password(password: str) -> str:
    return hashlib.sha256((password + SALT).encode()).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed


def create_token(user_id: int, role: str, username: str) -> str:
    payload = {
        "user_id": user_id,
        "role": role,
        "username": username,
        "exp": int(time.time()) + JWT_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token 已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的 Token")


async def get_current_user(request: Request) -> dict:
    """FastAPI 依赖：从 Authorization header 中提取并验证用户信息"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供有效的认证令牌")
    token = auth_header[7:]
    return decode_token(token)


async def require_admin(request: Request) -> dict:
    """FastAPI 依赖：要求管理员角色"""
    user = await get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
