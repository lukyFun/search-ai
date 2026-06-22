"""
轻量审计存储：单文件 SQLite (WAL 模式)。

替代之前的 Mongo `audit_logs` / `feedback_logs`。
- 一个进程一个长连接 + asyncio.Lock 串行化写入，单实例 demo 足够。
- 表 schema 见 _SCHEMA_SQL；启动时 idempotent 建表。
- 数据库文件由 settings.AUDIT_DB_PATH 指定（默认 data/audit.db），不入 repo。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

import aiosqlite

from app.core.config import get_settings
from app.core.logger import logger


settings = get_settings()


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS qa_audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id  TEXT,
  session_id  TEXT,
  query       TEXT,
  answer      TEXT,
  client_ip   TEXT,
  cached      INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_audit_created_at ON qa_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_qa_audit_session    ON qa_audit(session_id);
CREATE INDEX IF NOT EXISTS idx_qa_audit_client_ip  ON qa_audit(client_ip);

CREATE TABLE IF NOT EXISTS user_feedback (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id  TEXT,
  vote        TEXT NOT NULL,
  reason      TEXT,
  session_id  TEXT,
  query       TEXT,
  client_ip   TEXT,
  user_agent  TEXT,
  created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON user_feedback(created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_vote       ON user_feedback(vote);
"""


class _AuditDB:
    def __init__(self) -> None:
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        if self._conn is not None:
            return
        path = settings.AUDIT_DB_PATH
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = await aiosqlite.connect(path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        # executescript 不接受参数，建表 SQL 是常量
        await conn.executescript(_SCHEMA_SQL)
        await conn.commit()
        self._conn = conn
        logger.info("audit_db_initialized", path=path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("audit_db not initialized")
        return self._conn

    async def insert_qa_audit(
        self,
        *,
        request_id: Optional[str],
        session_id: Optional[str],
        query: Optional[str],
        answer: Optional[str],
        client_ip: Optional[str],
        cached: bool,
    ) -> None:
        conn = self._require()
        now_ms = int(time.time() * 1000)
        async with self._lock:
            await conn.execute(
                "INSERT INTO qa_audit "
                "(request_id, session_id, query, answer, client_ip, cached, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (request_id, session_id, query, answer, client_ip, 1 if cached else 0, now_ms),
            )
            await conn.commit()

    async def insert_user_feedback(
        self,
        *,
        request_id: Optional[str],
        vote: str,
        reason: Optional[str],
        session_id: Optional[str],
        query: Optional[str],
        client_ip: Optional[str],
        user_agent: Optional[str],
    ) -> None:
        conn = self._require()
        now_ms = int(time.time() * 1000)
        async with self._lock:
            await conn.execute(
                "INSERT INTO user_feedback "
                "(request_id, vote, reason, session_id, query, client_ip, user_agent, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, vote, reason, session_id, query, client_ip, user_agent, now_ms),
            )
            await conn.commit()

    async def query_qa_audit(
        self,
        *,
        limit: int,
        offset: int,
        since_ms: Optional[int],
        until_ms: Optional[int],
        session_id: Optional[str],
        client_ip: Optional[str],
        keyword: Optional[str],
    ) -> list[dict[str, Any]]:
        conn = self._require()
        where: list[str] = []
        params: list[Any] = []
        if since_ms is not None:
            where.append("created_at >= ?"); params.append(int(since_ms))
        if until_ms is not None:
            where.append("created_at < ?"); params.append(int(until_ms))
        if session_id:
            where.append("session_id = ?"); params.append(session_id)
        if client_ip:
            where.append("client_ip = ?"); params.append(client_ip)
        if keyword:
            where.append("(query LIKE ? OR answer LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.extend([int(limit), int(offset)])
        sql = (
            "SELECT id, request_id, session_id, query, answer, client_ip, cached, created_at "
            f"FROM qa_audit {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        async with self._lock:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        return [
            {
                "id": r[0], "request_id": r[1], "session_id": r[2],
                "query": r[3], "answer": r[4], "client_ip": r[5],
                "cached": bool(r[6]), "created_at": r[7],
            }
            for r in rows
        ]

    async def query_user_feedback(
        self,
        *,
        limit: int,
        offset: int,
        since_ms: Optional[int],
        until_ms: Optional[int],
        vote: Optional[str],
        keyword: Optional[str],
    ) -> list[dict[str, Any]]:
        conn = self._require()
        where: list[str] = []
        params: list[Any] = []
        if since_ms is not None:
            where.append("created_at >= ?"); params.append(int(since_ms))
        if until_ms is not None:
            where.append("created_at < ?"); params.append(int(until_ms))
        if vote:
            where.append("vote = ?"); params.append(vote)
        if keyword:
            where.append("(query LIKE ? OR reason LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.extend([int(limit), int(offset)])
        sql = (
            "SELECT id, request_id, vote, reason, session_id, query, client_ip, user_agent, created_at "
            f"FROM user_feedback {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        async with self._lock:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        return [
            {
                "id": r[0], "request_id": r[1], "vote": r[2], "reason": r[3],
                "session_id": r[4], "query": r[5], "client_ip": r[6],
                "user_agent": r[7], "created_at": r[8],
            }
            for r in rows
        ]

    async def purge_expired(self, retention_days: int) -> tuple[int, int]:
        conn = self._require()
        cutoff_ms = int((time.time() - retention_days * 86400) * 1000)
        async with self._lock:
            cur = await conn.execute("DELETE FROM qa_audit WHERE created_at < ?", (cutoff_ms,))
            audit_deleted = cur.rowcount or 0
            await cur.close()
            cur = await conn.execute("DELETE FROM user_feedback WHERE created_at < ?", (cutoff_ms,))
            feedback_deleted = cur.rowcount or 0
            await cur.close()
            await conn.commit()
        return audit_deleted, feedback_deleted


audit_db = _AuditDB()


async def janitor_loop() -> None:
    interval = max(60, int(settings.AUDIT_JANITOR_INTERVAL_SECONDS))
    retention = max(1, int(settings.AUDIT_RETENTION_DAYS))
    while True:
        try:
            await asyncio.sleep(interval)
            audit_n, feedback_n = await audit_db.purge_expired(retention)
            if audit_n or feedback_n:
                logger.info(
                    "audit_janitor_purged",
                    qa_audit_deleted=audit_n,
                    user_feedback_deleted=feedback_n,
                    retention_days=retention,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("audit_janitor_failed")
