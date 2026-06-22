"""
爬虫元数据 SQLite 存储（替代之前的 Mongo `documents` 集合）。

设计与 sqlite_store.audit_db 相同：单文件 + WAL + asyncio.Lock 串行写。
数据库路径来自 settings.CRAWL_DB_PATH（默认 data/crawl_meta.db）。

时间戳统一用 epoch ms（int），方便和 audit.db 一致。

用途：
- 爬虫去重（content hash 比较，避免重复 embedding）
- URL 失效检测（本次任务未爬到的旧文档标记 deleted）
- 任务恢复后能从 (url, last_crawled_at) 看到完整爬取历史
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, AsyncIterator, Optional

import aiosqlite

from app.core.config import get_settings
from app.core.logger import logger


settings = get_settings()


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
  url             TEXT PRIMARY KEY,
  hash            TEXT,
  title           TEXT,
  content         TEXT,
  status          TEXT NOT NULL DEFAULT 'active',
  crawl_status    TEXT,
  error_msg       TEXT,
  last_crawled_at INTEGER NOT NULL,
  created_at      INTEGER NOT NULL,
  deleted_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_doc_status          ON documents(status);
CREATE INDEX IF NOT EXISTS idx_doc_last_crawled_at ON documents(last_crawled_at);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class _CrawlDB:
    def __init__(self) -> None:
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        if self._conn is not None:
            return
        path = settings.CRAWL_DB_PATH
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(_SCHEMA_SQL)
        await conn.commit()
        self._conn = conn
        logger.info("crawl_db_initialized", path=path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("crawl_db not initialized")
        return self._conn

    async def get_by_url(self, url: str) -> Optional[dict]:
        conn = self._require()
        async with self._lock:
            cur = await conn.execute(
                "SELECT url, hash, title, content, status, crawl_status, error_msg, "
                "last_crawled_at, created_at, deleted_at FROM documents WHERE url=?",
                (url,),
            )
            row = await cur.fetchone()
            await cur.close()
        return dict(row) if row else None

    async def _upsert(self, *, url: str, set_fields: dict) -> None:
        """通用 upsert：created_at 仅在初次 INSERT 时落，后续更新只覆盖业务字段。"""
        conn = self._require()
        now = _now_ms()
        # 必填默认
        set_fields.setdefault("status", "active")
        cols_full = ["url", "hash", "title", "content", "status",
                     "crawl_status", "error_msg", "last_crawled_at", "deleted_at"]
        values = [
            url,
            set_fields.get("hash"),
            set_fields.get("title"),
            set_fields.get("content"),
            set_fields.get("status"),
            set_fields.get("crawl_status"),
            set_fields.get("error_msg"),
            set_fields.get("last_crawled_at", now),
            set_fields.get("deleted_at"),
        ]
        # 冲突更新：只更新由本次提供的字段（用 excluded.* 即可，因为我们把所有字段都列了）
        update_cols = [c for c in cols_full if c != "url"]
        update_sql = ", ".join([f"{c}=excluded.{c}" for c in update_cols])
        sql = (
            f"INSERT INTO documents ({', '.join(cols_full + ['created_at'])}) "
            f"VALUES ({', '.join(['?'] * len(cols_full))}, ?) "
            f"ON CONFLICT(url) DO UPDATE SET {update_sql}"
        )
        async with self._lock:
            await conn.execute(sql, (*values, now))
            await conn.commit()

    async def upsert_success(self, *, url: str, title: str, content: str, doc_hash: str) -> None:
        await self._upsert(url=url, set_fields={
            "hash": doc_hash, "title": title, "content": content,
            "status": "active", "crawl_status": "success", "error_msg": None,
            "last_crawled_at": _now_ms(), "deleted_at": None,
        })

    async def upsert_empty(self, *, url: str) -> None:
        """页面抓取成功但正文为空（例如图片页）。"""
        await self._upsert(url=url, set_fields={
            "content": "", "status": "active", "crawl_status": "success",
            "error_msg": None, "last_crawled_at": _now_ms(), "deleted_at": None,
        })

    async def upsert_failed(self, *, url: str, error_msg: str) -> None:
        await self._upsert(url=url, set_fields={
            "crawl_status": "failed", "error_msg": error_msg,
            "last_crawled_at": _now_ms(),
        })

    async def iter_outdated_active(
        self, *, domain: str, last_crawled_before_ms: int
    ) -> AsyncIterator[dict]:
        """
        生成器：返回某域名下 last_crawled_at < threshold 且仍 active 的文档。
        SQLite 不支持 mongo regex，用 LIKE 匹配 'http://domain%' / 'https://domain%'。
        """
        conn = self._require()
        async with self._lock:
            cur = await conn.execute(
                "SELECT url, last_crawled_at, status FROM documents "
                "WHERE status='active' AND last_crawled_at < ? "
                "AND (url LIKE ? OR url LIKE ?)",
                (int(last_crawled_before_ms), f"http://{domain}%", f"https://{domain}%"),
            )
            rows = await cur.fetchall()
            await cur.close()
        for row in rows:
            yield dict(row)

    async def mark_deleted(self, url: str) -> None:
        conn = self._require()
        now = _now_ms()
        async with self._lock:
            await conn.execute(
                "UPDATE documents SET status='deleted', deleted_at=? WHERE url=?",
                (now, url),
            )
            await conn.commit()


crawl_db = _CrawlDB()
