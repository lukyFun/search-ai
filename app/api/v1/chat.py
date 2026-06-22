import asyncio
import json

from fastapi import APIRouter, Depends, Request, Query
from sse_starlette.sse import EventSourceResponse
from app.models.schemas import ChatRequest, ChatResponse, FeedbackRequest, FeedbackResponse
from app.services.llm_service import llm_service
from app.core.logger import request_id_context, logger
from app.core.security import acquire_quota, check_rate_limit, check_session_rate, check_dedup
from app.core.config import get_settings
from app.core.sqlite_store import audit_db

settings = get_settings()

# 1. 创建 APIRouter
# Router 类似于 Flask 的 Blueprint，用于将 API 分组
router = APIRouter()

# 2. 定义路由处理函数
# - response_model: 指定返回数据的结构 (Pydantic Model)，FastAPI 会自动过滤掉多余字段并生成文档
# - async def: FastAPI 原生支持异步，适合 IO 密集型任务 (如调用 LLM API, 查库)
@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(check_rate_limit)])
async def chat(request: Request, chat_request: ChatRequest): 
    """
    RAG 问答接口 (非流式)
    """
    client_ip = getattr(request.state, "real_ip", "") or (request.client.host if request.client else "")
    logger.info(
        "chat_request_received",
        session_id=chat_request.session_id,
        query_length=len(chat_request.query),
        client_ip=client_ip,
    )
    await check_session_rate(chat_request.session_id)
    await check_dedup(ip=client_ip, session_id=chat_request.session_id, query=chat_request.query)
    async with await acquire_quota(request, chat_request.session_id):
        result = await llm_service.get_answer(
            query=chat_request.query,
            session_id=chat_request.session_id,
            client_ip=client_ip,
        )
    return ChatResponse(
        answer=result["answer"],
        sources=result["sources"],
        request_id=request_id_context.get()
    )

@router.get("/chat/stream", dependencies=[Depends(check_rate_limit)])
async def chat_stream(
    request: Request, 
    query: str = Query(..., min_length=1), 
    session_id: str = None
):
    """
    RAG 问答接口 (流式 SSE)
    
    使用 GET 请求，参数通过 query string 传递 (例如 /chat/stream?query=hello&session_id=...)
    返回 text/event-stream 格式
    """
    client_ip = getattr(request.state, "real_ip", "") or (request.client.host if request.client else "")
    logger.info(
        "chat_stream_request_received",
        session_id=session_id,
        query_length=len(query),
        client_ip=client_ip,
    )
    # 限流必须在响应头发出去之前完成，否则 SSE 已经开了流，前端只能收到空数据
    await check_session_rate(session_id)
    # 注意：SSE 不走 check_dedup —— 浏览器 EventSource 失败后会自动重连同样的 URL，
    # 与去重窗口形成 429 死循环。LLMService 里已有"上一轮 history 短路"，
    # 重复提问会直接返回缓存答案、不调 LLM，本身就是廉价的，不需要 dedup 兜底。
    # 防爬库重放交给 POST /chat 那条路径处理。

    async def _raw_event_generator():
        try:
            async with await acquire_quota(request, session_id):
                # 1. Manual Length Check for Graceful Error Handling
                # Instead of strict Pydantic 422 error (which breaks EventSource),
                # we check manually and yield an error event.
                if len(query) > settings.MAX_INPUT_LENGTH:
                    yield {
                        "event": "server_error",
                        "data": f"Input too long. Maximum {settings.MAX_INPUT_LENGTH} characters allowed.",
                    }
                    return

                request_id = request_id_context.get()
                yield {"event": "metadata", "data": json.dumps({"request_id": request_id})}

                async for chunk in llm_service.get_answer_stream(query, session_id, client_ip):
                    # chunk 格式: {"type": "content"|"sources"|"error"|"suggested_prompts", "data": ...}
                    # SSE 格式要求 data 必须是字符串
                    yield {"event": chunk["type"], "data": json.dumps(chunk["data"])}
        except Exception as e:
            # 捕获生成器内部的任何未处理异常
            # 确保前端收到错误通知而不是一直挂起
            logger.exception("chat_stream_generator_failed", error=str(e))
            yield {"event": "server_error", "data": f"Internal Server Error: {str(e)}"}

    async def event_generator():
        """
        包一层 chunk 间隔超时：每个 chunk 等待 <= SSE_CHUNK_TIMEOUT_SECONDS，
        超过即主动断开（防"挂连接耗资源"攻击 + 防上游 LLM 卡死）。
        """
        timeout = float(settings.SSE_CHUNK_TIMEOUT_SECONDS)
        raw_iter = _raw_event_generator().__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(raw_iter.__anext__(), timeout=timeout)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                logger.warning("sse_chunk_timeout", session_id=session_id, timeout=timeout)
                yield {"event": "server_error", "data": "Stream timeout."}
                try:
                    await raw_iter.aclose()
                except Exception:
                    pass
                return
            yield chunk

    return EventSourceResponse(event_generator())

@router.delete("/chat/session/{session_id}")
async def delete_session(session_id: str):
    """
    清除会话历史
    """
    await llm_service._delete_history(session_id)
    return {"status": "ok", "message": "Session history deleted"}


@router.post("/feedback", response_model=FeedbackResponse, dependencies=[Depends(check_rate_limit)])
async def submit_feedback(request: Request, payload: FeedbackRequest):
    """
    反馈双写：
    - structlog 关键字日志 event=user_feedback
    - SQLite user_feedback 表（/internal API 可分页查询）
    """
    client_ip = getattr(request.state, "real_ip", "") or (request.client.host if request.client else "")
    user_agent = request.headers.get("user-agent", "")
    await check_session_rate(payload.session_id)
    logger.info(
        "user_feedback",
        request_id=payload.request_id,
        vote=payload.vote,
        reason=payload.reason,
        session_id=payload.session_id,
        query=payload.query,
        client_ip=client_ip,
        user_agent=user_agent,
    )
    try:
        await audit_db.insert_user_feedback(
            request_id=payload.request_id,
            vote=payload.vote,
            reason=payload.reason,
            session_id=payload.session_id,
            query=payload.query,
            client_ip=client_ip,
            user_agent=user_agent,
        )
        return FeedbackResponse(status="ok")
    except Exception:
        logger.exception("user_feedback_db_write_failed")
        return FeedbackResponse(status="failed")
