import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from src.loader import config_data
from src.api.routes import router as api_router
from src.core.scheduler import FIFOScheduler
from src.models.database import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Lifecycle] Initializing SQLite Database tables...")
    await init_db()

    print("[Lifecycle] Starting background sandbox battery simulation task...")
    task = asyncio.create_task(app.state.scheduler.simulate_battery_growth())
    
    yield
    task.cancel()

app = FastAPI(
    title="智能充电桩调度计费系统",
    description="波普特大学（BUPT）智能充电桩管理后端 (增量I - 数据库版)",
    version="0.2.0",
    lifespan=lifespan
)

# 挂载单例调度机
app.state.scheduler = FIFOScheduler(config_data)

app.include_router(api_router, prefix="/api")

# 直接由后端服务器拦截并静态代理 HTML 和前端页面资源作为看板系统
app.mount("/", StaticFiles(directory="src/frontend", html=True), name="frontend")
