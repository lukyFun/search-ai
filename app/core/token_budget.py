"""
全局每日 LLM token 预算。

防止"账单攻击"（攻击者目的不是拿数据，是烧公司钱）：
- 每次 LLM 响应里的 usage.total_tokens 累加到当天计数
- 调 LLM 前先 check_budget()，若当天累计 >= 预算 → 直接拒答，不再调 LLM
- 按 UTC 日切换，跨天自动归零
- DAILY_TOKEN_BUDGET=0 表示不启用（开发态默认）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.logger import logger


settings = get_settings()


class DailyTokenBudgetExceeded(Exception):
    """全局每日 LLM token 预算耗尽。上层应回退到友好拒答，不再调 LLM。"""


class _DailyTokenMeter:
    def __init__(self) -> None:
        self._date_key: str = ""
        self._used: int = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def _today_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    async def _roll_if_new_day(self) -> None:
        key = self._today_key()
        if key != self._date_key:
            if self._date_key:
                logger.info(
                    "daily_token_budget_rollover",
                    previous_date=self._date_key,
                    previous_total_tokens=self._used,
                )
            self._date_key = key
            self._used = 0

    async def check_budget(self) -> None:
        """调 LLM 前检查；超额抛 DailyTokenBudgetExceeded。"""
        budget = int(settings.DAILY_TOKEN_BUDGET)
        if budget <= 0:
            return
        async with self._lock:
            await self._roll_if_new_day()
            if self._used >= budget:
                logger.warning(
                    "daily_token_budget_exhausted",
                    date=self._date_key, used=self._used, budget=budget,
                )
                raise DailyTokenBudgetExceeded()

    async def record_usage(self, tokens: int) -> None:
        try:
            t = int(tokens or 0)
        except Exception:
            return
        if t <= 0:
            return
        async with self._lock:
            await self._roll_if_new_day()
            self._used += t

    async def snapshot(self) -> dict:
        async with self._lock:
            await self._roll_if_new_day()
            return {
                "date": self._date_key,
                "used_tokens": self._used,
                "budget": int(settings.DAILY_TOKEN_BUDGET),
            }


token_meter = _DailyTokenMeter()
