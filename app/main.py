import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import get_settings
from app.core.logger import configure_logger, RequestIDMiddleware
from app.core.sqlite_store import audit_db, janitor_loop as audit_janitor_loop
from app.core.crawl_store import crawl_db
from app.core.cache import janitor_loop as cache_janitor_loop
from app.core.exceptions import AppError, app_exception_handler, generic_exception_handler
from app.api.v1 import chat, ingest, internal

settings = get_settings()

# 配置日志 (初始化 structlog)
configure_logger()

# 1. 定义 lifespan (生命周期) 管理器
# FastAPI 推荐使用 lifespan 来替代旧版的 @app.on_event("startup") 和 @app.on_event("shutdown")
# 这是一个异步上下文管理器，yield 之前的代码在应用启动时运行，yield 之后的代码在应用关闭时运行
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup (启动阶段) ---
    # SQLite：审计（qa_audit / user_feedback）+ 爬虫元数据（documents）
    await audit_db.init()
    await crawl_db.init()
    audit_janitor_task = asyncio.create_task(audit_janitor_loop())
    # 进程内缓存统一清理（LLM cache / IP rate / IP sessions / 等）
    cache_janitor_task = asyncio.create_task(cache_janitor_loop())

    # 预加载向量服务
    # 这会触发 BGE-M3 模型的加载（这是一个耗时操作），我们希望在应用启动时就完成它
    # 这样第一个用户请求就不会因为加载模型而卡顿
    from app.services.vector_service import get_vector_service
    get_vector_service()

    # 将控制权交给应用
    yield

    # --- Shutdown (关闭阶段) ---
    for t in (audit_janitor_task, cache_janitor_task):
        t.cancel()
    for t in (audit_janitor_task, cache_janitor_task):
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    await audit_db.close()
    await crawl_db.close()

# 2. 初始化 FastAPI 应用
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan, # 注入生命周期管理器
    docs_url="/docs",  # Swagger UI 地址
    redoc_url="/redoc", # ReDoc 地址
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# 3. 添加中间件 (Middleware)
# Middleware 就像洋葱皮，包裹在核心处理逻辑外面
# 请求进来时先经过 middleware，响应出去时也最后经过 middleware

# RequestIDMiddleware: 自定义的中间件，用于给每个请求生成唯一的 Request ID，方便日志追踪
app.add_middleware(RequestIDMiddleware)

# CORSMiddleware: 处理跨域资源共享 (CORS)
# 允许前端从不同域名访问此 API (这里设置为允许所有，生产环境建议限制域名)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 4. 注册全局异常处理器
# 当代码中抛出异常时，FastAPI 会捕获并使用这里定义的 handler 来生成 HTTP 响应
app.add_exception_handler(AppError, app_exception_handler) # 捕获自定义业务异常
app.add_exception_handler(Exception, generic_exception_handler) # 捕获所有未处理的系统异常

# 5. 注册路由 (Routers)
# 将不同模块的 API 路由挂载到主应用上，使代码结构更清晰
app.include_router(chat.router, prefix=settings.API_V1_STR, tags=["chat"])
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["ingest"])
app.include_router(internal.router, prefix=settings.API_V1_STR, tags=["internal"])

# 6. 挂载静态文件
# 将 app/static 目录挂载到 /static 路径，用于提供 CSS, JS, Images 等文件
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# 7. 根路径处理
# 当访问 http://localhost:8100/ 时，直接返回 index.html 页面
@app.get("/")
async def read_index():
    return FileResponse("app/static/index.html")

# 健康检查接口 (通常用于 k8s 或负载均衡器的心跳检测)
@app.get("/health")
async def health_check():
    return {"status": "ok", "version": settings.VERSION}
