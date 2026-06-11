import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from src.loader import config_data
from src.api.routes import router as api_router
from src.api.auth_routes import router as auth_router
from src.api.user_routes import router as user_router
from src.api.admin_routes import router as admin_router
from src.api.frontend_compat import router as compat_router
from src.core.scheduler import SmartScheduler
from src.core.billing import init_billing_config
from src.models.database import init_db, seed_admin, seed_charging_piles


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Lifecycle] Initializing SQLite Database tables...")
    await init_db()
    await seed_admin()
    await seed_charging_piles(config_data)

    print("[Lifecycle] Initializing billing config from config.yaml...")
    init_billing_config(config_data)

    print("[Lifecycle] Restoring in-progress orders from database...")
    await app.state.scheduler.restore_from_db()

    print("[Lifecycle] Starting background tasks...")
    sim_task = asyncio.create_task(
        app.state.scheduler.simulate_battery_growth())
    # 仅当显式设置 SCS_DISABLE_WATCHER=1 时才禁用后台 dispatch_watcher
    # 默认行为 (无环境变量) 保持原样, 不影响 G8 等验收
    import os
    if os.environ.get("SCS_DISABLE_WATCHER", "0") == "1":
        print("[Lifecycle] SCS_DISABLE_WATCHER=1 — dispatch_watcher 已禁用 (供 G9 策略对比实验用)")
        dispatch_task = None
    else:
        dispatch_task = asyncio.create_task(
            app.state.scheduler.dispatch_watcher())

    yield

    sim_task.cancel()
    if dispatch_task is not None:
        dispatch_task.cancel()


app = FastAPI(
    title="智能充电桩调度计费系统",
    description="BUPT 智能充电桩管理后端 — 完整需求版",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载调度器单例
app.state.scheduler = SmartScheduler(config_data)

# 路由挂载
app.include_router(auth_router, prefix="/api/auth", tags=["认证"])
app.include_router(user_router, prefix="/api/user", tags=["用户"])
app.include_router(admin_router, prefix="/api/admin", tags=["管理员"])
app.include_router(api_router, prefix="/api", tags=["兼容"])
app.include_router(compat_router, prefix="/api", tags=["前端降权兼容"])

# 前端静态文件（放在最后，不覆盖 API 路由）
app.mount("/", StaticFiles(directory="src/frontend", html=True),
          name="frontend")
