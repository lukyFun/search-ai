from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from app.core.logger import logger

class AppError(Exception):
    def __init__(self, message: str, code: int = 500):
        self.message = message
        self.code = code

async def app_exception_handler(request: Request, exc: AppError):
    logger.error("app_error", error=exc.message, code=exc.code)
    return JSONResponse(
        status_code=exc.code,
        content={"message": exc.message, "code": exc.code},
    )

async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "unhandled_error",
        error=str(exc),
        path=request.url.path,
        method=request.method,
    )
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )
