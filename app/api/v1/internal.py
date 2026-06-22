"""
内部诊断 / 审计查询 API（仅限本地访问）。

- 通过 INTERNAL_API_ALLOWED_IPS 白名单（支持 CIDR）控制
- include_in_schema=False，不在 /docs 暴露
- 用于查 audit / feedback、看缓存 size，方便研发排查与人工审计
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.core.cache import stats as cache_stats
from app.core.config import get_settings
from app.core.net import ip_in_networks, parse_ip_list
from app.core.sqlite_store import audit_db
from app.core.token_budget import token_meter


settings = get_settings()
router = APIRouter(include_in_schema=False)


def _ensure_local(request: Request) -> None:
    allowed = parse_ip_list(settings.INTERNAL_API_ALLOWED_IPS)
    client_ip = request.client.host if request.client else ""
    # 注：internal API 不接受 X-Forwarded-For，直接用 transport IP，
    # 否则攻击者通过伪造 XFF 即可绕过白名单。
    if not ip_in_networks(client_ip, allowed):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


@router.get("/internal/feedback")
async def list_feedback(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    vote: Optional[str] = Query(None, pattern="^(up|down)$"),
    keyword: Optional[str] = Query(None, max_length=200),
):
    _ensure_local(request)
    items = await audit_db.query_user_feedback(
        limit=limit, offset=offset,
        since_ms=since_ms, until_ms=until_ms,
        vote=vote, keyword=keyword,
    )
    return {"items": items, "limit": limit, "offset": offset, "count": len(items)}


@router.get("/internal/audit")
async def list_audit(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    session_id: Optional[str] = Query(None, max_length=128),
    client_ip: Optional[str] = Query(None, max_length=64),
    keyword: Optional[str] = Query(None, max_length=200),
):
    _ensure_local(request)
    items = await audit_db.query_qa_audit(
        limit=limit, offset=offset,
        since_ms=since_ms, until_ms=until_ms,
        session_id=session_id, client_ip=client_ip, keyword=keyword,
    )
    return {"items": items, "limit": limit, "offset": offset, "count": len(items)}


@router.get("/internal/cache-stats")
async def get_cache_stats(request: Request):
    """各内存缓存的 size 概览，便于排查"内存涨没涨"问题。"""
    _ensure_local(request)
    return {"caches": cache_stats()}


@router.get("/internal/token-budget")
async def get_token_budget(request: Request):
    """查看当前 UTC 日已用 / 预算 token 数。"""
    _ensure_local(request)
    return await token_meter.snapshot()
