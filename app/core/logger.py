import os
import sys
import uuid
import structlog
import logging
import logging.handlers
import time
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from contextvars import ContextVar
from app.core.config import get_settings

# ContextVar 用于存储 request_id，以便在日志中访问
request_id_context: ContextVar[str] = ContextVar("request_id", default="N/A")

def configure_logger():
    """
    配置 Structlog 和标准 Logging，使其输出 JSON 格式且包含 request_id
    """
    
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    settings = get_settings()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)

    # 文件日志滚动 —— RotatingFileHandler 防止 .run/app.log 无限增长
    log_path = settings.LOG_FILE
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=int(settings.LOG_MAX_BYTES),
        backupCount=int(settings.LOG_BACKUP_COUNT),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    handlers = [stdout_handler, file_handler]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 强制覆盖 root handler，让 uvicorn/httpx 等标准日志也走统一 JSON 输出。
    root_logger = logging.getLogger()
    root_logger.handlers = list(handlers)
    root_logger.setLevel(logging.INFO)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
        std_logger = logging.getLogger(logger_name)
        std_logger.handlers = list(handlers)
        std_logger.propagate = False
        std_logger.setLevel(logging.INFO)

    # chromadb 的 telemetry 在本地离线开发常产生噪音错误，直接静音。
    for logger_name in ("chromadb.telemetry", "chromadb.telemetry.product.posthog"):
        noise_logger = logging.getLogger(logger_name)
        noise_logger.handlers = list(handlers)
        noise_logger.propagate = False
        noise_logger.setLevel(logging.CRITICAL)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request_id_context.set(request_id)

        # 解析真实客户端 IP（X-Forwarded-For + TRUSTED_PROXIES），存到 request.state，
        # 下游 security / chat / audit 统一从 request.state.real_ip 取。
        from app.core.net import parse_ip_list, real_client_ip
        trusted = parse_ip_list(get_settings().TRUSTED_PROXIES)
        request.state.real_ip = real_client_ip(request, trusted)

        # 绑定到 structlog 上下文
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        logger.info(
            "request_started",
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            client_ip=request.state.real_ip,
        )

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise
        else:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request_finished",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
            return response
        finally:
            structlog.contextvars.clear_contextvars()

# 获取 logger 实例
logger = structlog.get_logger()
