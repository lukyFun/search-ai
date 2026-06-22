"""
单实例容器的内存配额与限流。

设计原则：所有按 IP/session_id 为键的容器都必须有
  (a) 上限   —— TtlLruCache 的 max_size
  (b) TTL    —— default_ttl_seconds
  (c) 后台清理 —— 注册到 cache.janitor_loop

限流维度（任一超阈值即 429）：
  - 单 IP RPM         —— RATE_LIMIT_PER_MINUTE
  - 单 /24 子网 RPM   —— RATE_LIMIT_PER_MINUTE_PER_SUBNET（防代理池）
  - 全局 RPM          —— GLOBAL_RATE_LIMIT_PER_MINUTE（兜底）
  - 单 session RPM    —— RATE_LIMIT_PER_MINUTE_PER_SESSION（防 IP 切换但保留 session）

所有拒答 raise 前会强制 sleep REJECT_RESPONSE_FLOOR_SECONDS（默认 200ms），
让"快速被拦截"和"正常处理"的响应时间差异变小，提高侧信道试探成本。

多实例需共享存储或网关层做。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import time
from typing import Dict, Optional

from fastapi import HTTPException, Request, status

from app.core.cache import SlidingWindowCounter, TtlLruCache
from app.core.config import get_settings
from app.core.logger import logger


settings = get_settings()


_IP_MAP_MAX_SIZE = 10000
_IP_SEM_IDLE_TTL = 600
_IP_SESSIONS_TTL = 1800
_SESSION_TTL_SECONDS = 1800
_GLOBAL_KEY = "__global__"


def _subnet_key(ip: str) -> str:
    """IPv4 取 /24，IPv6 取 /64。无效 IP 退化为原值。"""
    if not ip:
        return ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv4Address):
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    else:
        net = ipaddress.ip_network(f"{ip}/64", strict=False)
    return str(net.network_address)


async def _reject(detail: str, log_event: str, **log_fields) -> None:
    """统一拒答路径：先 sleep 到下限再抛 429，抹平响应时间侧信道。"""
    logger.info(log_event, **log_fields)
    floor = float(settings.REJECT_RESPONSE_FLOOR_SECONDS)
    if floor > 0:
        await asyncio.sleep(floor)
    raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)


class _Quota:
    def __init__(self):
        self._global_sem = asyncio.Semaphore(settings.MAX_CONCURRENT_QA)
        self._ip_sems: TtlLruCache[str, asyncio.Semaphore] = TtlLruCache(
            "ip_concurrency_sems", max_size=_IP_MAP_MAX_SIZE, default_ttl_seconds=_IP_SEM_IDLE_TTL
        )
        self._ip_session_ids: TtlLruCache[str, Dict[str, float]] = TtlLruCache(
            "ip_sessions", max_size=_IP_MAP_MAX_SIZE, default_ttl_seconds=_IP_SESSIONS_TTL
        )
        # 限流：四个独立滑动窗口
        self._ip_rate = SlidingWindowCounter(
            "ip_rate_per_min", max_keys=_IP_MAP_MAX_SIZE, window_seconds=60
        )
        self._subnet_rate = SlidingWindowCounter(
            "subnet_rate_per_min", max_keys=_IP_MAP_MAX_SIZE, window_seconds=60
        )
        self._session_rate = SlidingWindowCounter(
            "session_rate_per_min", max_keys=_IP_MAP_MAX_SIZE, window_seconds=60
        )
        # 单 key counter，承载全局 RPM
        self._global_rate = SlidingWindowCounter(
            "global_rate_per_min", max_keys=4, window_seconds=60
        )
        self._ip_new_sessions = SlidingWindowCounter(
            "ip_new_sessions_per_min", max_keys=_IP_MAP_MAX_SIZE, window_seconds=60
        )

    async def check_request_rate_limits(self, ip: str) -> None:
        """请求维度的限流：IP / /24 子网 / 全局，任一超阈值就拒。"""
        # 全局兜底
        g = await self._global_rate.incr_and_count(_GLOBAL_KEY)
        if g > settings.GLOBAL_RATE_LIMIT_PER_MINUTE:
            await _reject(
                "Service is busy. Try again later.",
                "rate_limit_global", count=g, limit=settings.GLOBAL_RATE_LIMIT_PER_MINUTE,
            )

        # /24 子网（IPv6 用 /64）
        subnet = _subnet_key(ip)
        if subnet:
            s = await self._subnet_rate.incr_and_count(subnet)
            if s > settings.RATE_LIMIT_PER_MINUTE_PER_SUBNET:
                await _reject(
                    "Too many requests from your network.",
                    "rate_limit_subnet", subnet=subnet, count=s,
                    limit=settings.RATE_LIMIT_PER_MINUTE_PER_SUBNET,
                )

        # 单 IP
        if ip:
            n = await self._ip_rate.incr_and_count(ip)
            if n > settings.RATE_LIMIT_PER_MINUTE:
                await _reject(
                    "Too many requests.",
                    "rate_limit_ip", ip=ip, count=n, limit=settings.RATE_LIMIT_PER_MINUTE,
                )

    async def check_session_rate_limit(self, session_id: Optional[str]) -> None:
        if not session_id:
            return
        n = await self._session_rate.incr_and_count(str(session_id))
        if n > settings.RATE_LIMIT_PER_MINUTE_PER_SESSION:
            await _reject(
                "Too many requests for this session.",
                "rate_limit_session", session_id=session_id, count=n,
                limit=settings.RATE_LIMIT_PER_MINUTE_PER_SESSION,
            )

    async def _register_session(self, ip: str, session_id: Optional[str]) -> None:
        if not session_id:
            return
        session_id = str(session_id)
        now = time.monotonic()

        sessions = await self._ip_session_ids.get(ip)
        if sessions is None:
            sessions = {}

        for sid in [sid for sid, exp in sessions.items() if exp <= now]:
            sessions.pop(sid, None)

        if session_id not in sessions:
            if len(sessions) >= settings.MAX_SESSIONS_PER_IP:
                await _reject(
                    "Too many sessions from this IP.",
                    "session_quota_exhausted_ip", ip=ip, limit=settings.MAX_SESSIONS_PER_IP,
                )
            new_count = await self._ip_new_sessions.incr_and_count(ip)
            if new_count > settings.MAX_NEW_SESSIONS_PER_MINUTE:
                await _reject(
                    "Too many new sessions from this IP.",
                    "session_creation_throttled_ip", ip=ip, count=new_count,
                    limit=settings.MAX_NEW_SESSIONS_PER_MINUTE,
                )

        sessions[session_id] = now + _SESSION_TTL_SECONDS
        await self._ip_session_ids.set(ip, sessions)

    async def acquire(self, ip: str, session_id: Optional[str]):
        await self._register_session(ip, session_id)
        sem = await self._ip_sems.get_or_create(
            ip,
            factory=lambda: asyncio.Semaphore(settings.MAX_CONCURRENT_QA_PER_IP),
        )
        await self._global_sem.acquire()
        try:
            await sem.acquire()
        except BaseException:
            self._global_sem.release()
            raise
        return _QuotaLease(self, ip, sem)

    async def release(self, ip: str, sem: asyncio.Semaphore) -> None:
        sem.release()
        self._global_sem.release()


class _QuotaLease:
    def __init__(self, quota: _Quota, ip: str, sem: asyncio.Semaphore):
        self._quota = quota
        self._ip = ip
        self._sem = sem

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._quota.release(self._ip, self._sem)


quota = _Quota()


# 同 (session/ip, query_hash) 短窗口去重，防脚本爬库 / 同请求重放
_dedup_cache: TtlLruCache[str, int] = TtlLruCache(
    "request_dedup", max_size=_IP_MAP_MAX_SIZE,
    default_ttl_seconds=float(settings.DEDUP_WINDOW_SECONDS) or 5.0,
)


async def check_dedup(*, ip: str, session_id: Optional[str], query: str) -> None:
    """短窗口去重：相同 query 在 DEDUP_WINDOW_SECONDS 内再次到达即 429。

    优先用 session_id 当 key（同 session 重发更明显），退化到 IP（无 session 时仍能防）。
    """
    window = float(settings.DEDUP_WINDOW_SECONDS)
    if window <= 0 or not query:
        return
    key_base = session_id or ip
    if not key_base:
        return
    qhash = hashlib.sha256(query.encode("utf-8", errors="ignore")).hexdigest()[:16]
    key = f"{key_base}:{qhash}"
    if await _dedup_cache.get(key) is not None:
        await _reject(
            "Duplicate request, please slow down.",
            "dedup_hit", key_base=key_base[:40],
        )
    await _dedup_cache.set(key, 1, ttl_seconds=window)


def _client_ip(request: Request) -> str:
    ip = getattr(request.state, "real_ip", "")
    if ip:
        return ip
    return request.client.host if request.client else ""


async def check_rate_limit(request: Request):
    """FastAPI Depends：检查 IP / 子网 / 全局 RPM。session 维度需端点内显式调用 check_session_rate。"""
    try:
        await quota.check_request_rate_limits(_client_ip(request))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("rate_limit_internal_error", error=str(e))


async def check_session_rate(session_id: Optional[str]) -> None:
    """端点内显式调用，校验单 session_id 每分钟请求数。"""
    await quota.check_session_rate_limit(session_id)


async def acquire_quota(request: Request, session_id: Optional[str]):
    return await quota.acquire(_client_ip(request), session_id)
