from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from src.api.schemas import UserRegister, UserLogin, TokenResponse, UserInfo
from src.api.auth import hash_password, verify_password, create_token, get_current_user
from src.models.database import AsyncSessionLocal
from src.models.models import User
from fastapi import Depends

router = APIRouter()


@router.post("/register", response_model=TokenResponse)
async def register(body: UserRegister):
    """用户注册"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.username == body.username)
        )
        if result.scalars().first() is not None:
            raise HTTPException(status_code=400, detail="用户名已存在")

        user = User(
            username=body.username,
            password_hash=hash_password(body.password),
            role="user",
            vehicle_id=body.vehicle_id,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    token = create_token(user.id, user.role, user.username)
    return TokenResponse(
        access_token=token,
        role=user.role,
        user_id=user.id,
        username=user.username,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin):
    """用户登录"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.username == body.username)
        )
        user = result.scalars().first()

    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_token(user.id, user.role, user.username)
    return TokenResponse(
        access_token=token,
        role=user.role,
        user_id=user.id,
        username=user.username,
    )


@router.get("/me", response_model=UserInfo)
async def get_me(current_user: dict = Depends(get_current_user)):
    """获取当前用户信息"""
    return UserInfo(
        user_id=current_user["user_id"],
        username=current_user["username"],
        role=current_user["role"],
    )
