# database.py
"""
MySQL 异步存储（aiomysql）
功能：初始化表结构、保存聊天记录、查询历史、清空会话、统计信息
特性：禁用自动提交，写操作手动 commit / rollback
"""
import os
import json
from typing import List, Dict, Any, Optional
from datetime import datetime

import aiomysql
from dotenv import load_dotenv, find_dotenv


class ChatSqlDatabase:

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        database: str = "chatdb",
        minsize: int = 1,
        maxsize: int = 5,
    ):
        # 加载 .env 并设置API环境变量（不覆盖已存在的环境变量）
        try:
            load_dotenv(find_dotenv(), override=True)
        except Exception:
            # 忽略 .env 加载错误，继续从系统环境读取
            pass
        # 从.env中读取环境变量
        self.cfg = {
            "host": os.getenv("MYSQL_HOST", "").strip(),
            "port": int(os.getenv("MYSQL_PORT", "3306")),
            "user": os.getenv("MYSQL_USER", "").strip(),
            "password": os.getenv("MYSQL_PASSWORD", "").strip(),
            "db": os.getenv("HISTORY_DATABASE", "").strip(),
            "charset": "utf8mb4",
            "autocommit": False,  # 关键：禁用自动提交
            "minsize": minsize,
            "maxsize": maxsize,
        }
        self.pool: Optional[aiomysql.Pool] = None

    async def _ensure_pool(self):
        if not self.pool:
            self.pool = await aiomysql.create_pool(
                host=self.cfg["host"],
                port=self.cfg["port"],
                user=self.cfg["user"],
                password=self.cfg["password"],
                db=self.cfg["db"],
                minsize=self.cfg["minsize"],
                maxsize=self.cfg["maxsize"],
                charset=self.cfg["charset"],
                autocommit=self.cfg["autocommit"],  # 关闭自动提交
            )

    async def initialize(self) -> bool:
        """
        初始化表结构（若不存在则创建）。
        仅创建与最小功能相关的列；JSON 列用于存储工具调用与结果。
        """
        try:
            await self._ensure_pool()
            async with self.pool.acquire() as conn:
                try:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS chat_sessions (
                                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                                session_id VARCHAR(191) NOT NULL UNIQUE,
                                user_id VARCHAR(191) NOT NULL,
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                KEY idx_user (user_id)
                            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                            """
                        )
                        await cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS chat_records (
                                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                                session_id VARCHAR(191) NOT NULL,
                                user_input LONGTEXT,
                                user_timestamp VARCHAR(64),
                                mcp_tools_called JSON NULL,
                                mcp_results JSON NULL,
                                ai_response LONGTEXT,
                                ai_timestamp VARCHAR(64),
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                CONSTRAINT fk_session
                                    FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id)
                                    ON UPDATE CASCADE ON DELETE CASCADE,
                                KEY idx_session (session_id),
                                KEY idx_created (created_at)
                            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                            """
                        )
                    await conn.commit()
                    return True
                except Exception:
                    await conn.rollback()
                    raise
        except Exception as e:
            print(f"❌ 初始化失败: {e}")
            return False

    async def save_conversation(
        self,
        user_input: str,
        mcp_tools_called: List[Dict[str, Any]] | None = None,
        mcp_results: List[Dict[str, Any]] | None = None,
        ai_response: str = "",
        session_id: str = "default",
        user_id: str = "default",
    ) -> bool:
        """
        单次保存：确保 session 存在 + 插入一条聊天记录。
        两步置于同一事务中，任何一步失败都回滚。
        """
        mcp_tools_json = json.dumps(mcp_tools_called or [], ensure_ascii=False)
        mcp_results_json = json.dumps(mcp_results or [], ensure_ascii=False)
        now = datetime.now().isoformat()

        await self._ensure_pool()
        async with self.pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    # 确保会话存在（唯一约束避免重复）
                    #todo insert 语句可能后续需要更改
                    await cur.execute(
                        "INSERT IGNORE INTO chat_sessions (session_id, user_id) VALUES (%s, %s)",
                        (session_id, user_id),
                    )
                    # 插入记录
                    await cur.execute(
                        """
                        INSERT INTO chat_records (
                            session_id,
                            user_input, user_timestamp,
                            mcp_tools_called, mcp_results,
                            ai_response, ai_timestamp
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            session_id,
                            user_input, now,
                            mcp_tools_json, mcp_results_json,
                            ai_response, now,
                        ),
                    )
                await conn.commit()
                return True
            except Exception as e:
                await conn.rollback()
                print(f"❌ 保存失败: {e}")
                return False

    async def get_chat_history(self, session_id: str = "default", limit: int = 50) -> List[Dict[str, Any]]:
        """按时间升序返回指定会话的最近若干条记录。"""
        await self._ensure_pool()
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, session_id,
                           user_input, user_timestamp,
                           mcp_tools_called, mcp_results,
                           ai_response, ai_timestamp, created_at
                      FROM chat_records
                     WHERE session_id = %s
                  ORDER BY created_at ASC
                     LIMIT %s
                    """,
                    (session_id, int(limit)),
                )
                rows = await cur.fetchall()
                cols = [d[0] for d in cur.description]

        records: List[Dict[str, Any]] = []
        for row in rows:
            rec = dict(zip(cols, row))
            # 解析 JSON 字段（aiomysql 取出可能是 str/bytes）
            for k in ("mcp_tools_called", "mcp_results"):
                v = rec.get(k)
                if v and not isinstance(v, list):
                    try:
                        rec[k] = json.loads(v)
                    except Exception:
                        rec[k] = []
                elif v is None:
                    rec[k] = []
            records.append(rec)
        return records

    async def clear_history(self, session_id: str = "default") -> bool:
        """删除指定会话及其所有记录（外键级联已启用）。"""
        await self._ensure_pool()
        async with self.pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    # 先删 session，会级联删除 records
                    await cur.execute("DELETE FROM chat_sessions WHERE session_id = %s", (session_id,))
                await conn.commit()
                return True
            except Exception as e:
                await conn.rollback()
                print(f"❌ 清空失败: {e}")
                return False

    async def get_stats(self) -> Dict[str, Any]:
        """返回最简统计信息：总记录数、会话数、最近记录时间"""
        await self._ensure_pool()
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM chat_records")
                total_records = (await cur.fetchone())[0]
                await cur.execute("SELECT COUNT(*) FROM chat_sessions")
                total_sessions = (await cur.fetchone())[0]
                await cur.execute("SELECT MAX(created_at) FROM chat_records")
                latest_record = (await cur.fetchone())[0]
        return {
            "total_records": total_records,
            "total_sessions": total_sessions,
            "latest_record": str(latest_record) if latest_record else None,
            "database": self.cfg["db"],
        }

    async def close(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
