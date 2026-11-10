"""
解析历史记录管理模块 (HistoryManager)
负责记录各用户提取历史、用户隔离查询、沙箱文件安全重新载入与删除
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_DB_PATH = Path(os.getenv("CP_APP_DB_PATH", ROOT_DIR / "data" / "app.db"))
OUTPUT_DIR = ROOT_DIR / "output"


class HistoryManager:
    """负责解析历史记录的持久化存储、用户隔离查询与历史还原管理"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS extraction_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    file_name TEXT NOT NULL,
                    standard_category TEXT DEFAULT '',
                    mode TEXT DEFAULT 'full',
                    engine_used TEXT DEFAULT '',
                    valid_count INTEGER DEFAULT 0,
                    pending_count INTEGER DEFAULT 0,
                    excel_url TEXT DEFAULT '',
                    json_url TEXT DEFAULT '',
                    source_file_url TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            # 索引加速
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_user ON extraction_history(user_id, id DESC)"
            )
            conn.commit()

    def record_extraction(
        self,
        user_id: int,
        file_name: str,
        standard_category: str,
        mode: str,
        engine_used: str,
        valid_count: int,
        pending_count: int,
        excel_url: str,
        json_url: str,
        source_file_url: str = "",
    ) -> Dict[str, Any]:
        """记录一次提取事件"""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO extraction_history (
                    user_id, file_name, standard_category, mode, engine_used,
                    valid_count, pending_count, excel_url, json_url, source_file_url, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    file_name,
                    standard_category,
                    mode,
                    engine_used,
                    valid_count,
                    pending_count,
                    excel_url,
                    json_url,
                    source_file_url,
                    now_str,
                ),
            )
            rec_id = cursor.lastrowid
            conn.commit()
            row = conn.execute("SELECT * FROM extraction_history WHERE id = ?", (rec_id,)).fetchone()
            return self._row_to_dict(row)

    def list_history(
        self,
        user_id: Optional[int] = None,
        is_admin: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询历史记录列表，严格按租户隔离（管理员可查全局）"""
        query = "SELECT * FROM extraction_history WHERE 1=1"
        params: List[Any] = []
        if not is_admin:
            query += " AND user_id = ?"
            params.append(user_id or 0)
        elif user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def count_history(self, user_id: Optional[int] = None, is_admin: bool = False) -> int:
        """获取当前用户的历史记录总数"""
        query = "SELECT COUNT(*) as cnt FROM extraction_history WHERE 1=1"
        params: List[Any] = []
        if not is_admin:
            query += " AND user_id = ?"
            params.append(user_id or 0)
        elif user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)

        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
            return row["cnt"] if row else 0

    def get_record(
        self,
        record_id: int,
        user_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """获取某条记录的元数据，若不属于当前用户且非管理员则阻断 (IDOR 防护)"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM extraction_history WHERE id = ?", (record_id,)).fetchone()
            if not row:
                return None
            data = self._row_to_dict(row)
            if not is_admin and data["user_id"] != user_id:
                raise PermissionError("越权访问拦截：无权查看其他工程师的专属提取历史")
            return data

    def reload_record_package(
        self,
        record_id: int,
        user_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> Dict[str, Any]:
        """从用户沙箱中加载历史提取生成的完整 package JSON 数据，供 Web 工作台一键重现与再次复核"""
        record = self.get_record(record_id, user_id=user_id, is_admin=is_admin)
        if not record:
            raise ValueError(f"历史记录 ID={record_id} 不存在")

        owner_id = record["user_id"]
        json_filename = Path(record["json_url"]).name
        target_path = OUTPUT_DIR / "users" / str(owner_id) / json_filename
        if not target_path.exists():
            target_path = OUTPUT_DIR / json_filename

        if not target_path.exists():
            raise FileNotFoundError(f"历史结果文件已不存在: {json_filename}")

        package_data = json.loads(target_path.read_text(encoding="utf-8"))
        return {
            "record": record,
            "package": package_data,
            "excel_download_url": record["excel_url"],
            "json_download_url": record["json_url"],
            "mode": record.get("mode", "full"),
            "engine_used": record.get("engine_used", ""),
        }

    def delete_record(
        self,
        record_id: int,
        user_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> bool:
        """删除历史记录并安全清理关联的沙箱产物文件"""
        record = self.get_record(record_id, user_id=user_id, is_admin=is_admin)
        if not record:
            return False

        owner_id = record["user_id"]
        for url_field in ("json_url", "excel_url"):
            url_val = record.get(url_field)
            if url_val:
                fname = Path(url_val).name
                user_file = OUTPUT_DIR / "users" / str(owner_id) / fname
                if user_file.exists():
                    try:
                        user_file.unlink()
                    except Exception:
                        pass

        with self._connect() as conn:
            conn.execute("DELETE FROM extraction_history WHERE id = ?", (record_id,))
            conn.commit()
            return True

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "file_name": row["file_name"],
            "standard_category": row["standard_category"],
            "mode": row["mode"],
            "engine_used": row["engine_used"],
            "valid_count": row["valid_count"],
            "pending_count": row["pending_count"],
            "excel_url": row["excel_url"],
            "json_url": row["json_url"],
            "source_file_url": row["source_file_url"] if "source_file_url" in row.keys() else "",
            "created_at": row["created_at"],
        }
