"""
用户认证、鉴权核心与数据管理模块
提供加盐密码哈希、基于 RFC 7519 规范的纯原生标准 JWT 签发与验证，以及 SQLite 用户持久化存储。
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..schemas.auth import UserResponse

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_APP_DB_PATH = Path(os.getenv("CP_APP_DB_PATH", ROOT_DIR / "data" / "app.db"))
DEFAULT_SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "cp-extractor-insecure-default-secret-key-2026")
JWT_ALGORITHM = "HS256"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64url_decode(data_str: str) -> bytes:
    padding = 4 - (len(data_str) % 4)
    if padding and padding < 4:
        data_str += "=" * padding
    return base64.urlsafe_b64decode(data_str.encode("utf-8"))


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """生成或使用已知盐计算 PBKDF2-HMAC-SHA256 密码哈希"""
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    ).hex()
    return hashed, salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """常量时间比较密码哈希（防时序侧信道攻击）"""
    computed, _ = hash_password(password, salt=salt)
    return hmac.compare_digest(computed, stored_hash)


def create_jwt_token(payload: Dict[str, Any], secret_key: str = DEFAULT_SECRET_KEY) -> str:
    """按 RFC 7519 标准签发 HS256 JWT Token"""
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    signature = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(signature)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


def decode_jwt_token(token: str, secret_key: str = DEFAULT_SECRET_KEY) -> Dict[str, Any]:
    """验证并解码 HS256 JWT Token，校验签名与过期时间"""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ValueError("无效的 Token 格式")

    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        expected_sig = _b64url_decode(sig_b64)
    except Exception:
        raise ValueError("Token 编码损坏或无法解析")

    if header.get("alg") != JWT_ALGORITHM:
        raise ValueError(f"不支持的签名算法: {header.get('alg')}")

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    actual_sig = hmac.new(secret_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(actual_sig, expected_sig):
        raise ValueError("Token 签名验证失败，已被篡改")

    exp = payload.get("exp")
    if exp is not None:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if now_ts > int(exp):
            raise ValueError("Token 已过期，请重新登录")

    return payload


class AuthManager:
    """用户认证与 SQLite 账号体系管理器"""

    def __init__(self, db_path: Optional[Path] = None, secret_key: str = DEFAULT_SECRET_KEY):
        self.db_path = Path(db_path) if db_path else DEFAULT_APP_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_key = secret_key
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_login_at TEXT DEFAULT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    is_admin INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # 兼容已有 users 表动态平滑迁移 is_admin 列
            existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "is_admin" not in existing_cols:
                conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

            # 默认系统管理员账号初始化：admin / admin
            admin_row = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
            if not admin_row:
                pwd_hash, salt = hash_password("admin")
                now_str = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO users (username, password_hash, salt, display_name, created_at, is_active, is_admin)
                    VALUES ('admin', ?, ?, '系统管理员', ?, 1, 1)
                    """,
                    (pwd_hash, salt, now_str),
                )
            else:
                conn.execute("UPDATE users SET is_admin = 1 WHERE username = 'admin'")

            conn.commit()

    def register_user(self, username: str, password: str, display_name: str = "") -> UserResponse:
        """注册新用户（默认普通权限）"""
        clean_username = username.strip()
        if not clean_username or len(clean_username) < 3:
            raise ValueError("用户名至少需要 3 个字符")
        if not re.match(r"^[a-zA-Z0-9_\-\u4e00-\u9fa5]+$", clean_username):
            raise ValueError("用户名只能包含中文字符、字母、数字、下划线及连字符")
        if len(password) < 5:
            raise ValueError("密码长度至少需要 5 位")

        pwd_hash, salt = hash_password(password)
        now_str = datetime.now(timezone.utc).isoformat()

        with self._connect() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO users (username, password_hash, salt, display_name, created_at, is_active, is_admin)
                    VALUES (?, ?, ?, ?, ?, 1, 0)
                    """,
                    (clean_username, pwd_hash, salt, display_name.strip(), now_str),
                )
                conn.commit()
                user_id = cursor.lastrowid
            except sqlite3.IntegrityError:
                raise ValueError(f"用户名 '{clean_username}' 已被注册，请更换其它用户名")

        return UserResponse(
            id=user_id,
            username=clean_username,
            display_name=display_name.strip(),
            created_at=now_str,
            last_login_at=None,
            is_active=True,
            is_admin=False,
        )

    def authenticate_user(self, username: str, password: str) -> UserResponse:
        """验证用户名与密码"""
        clean_username = username.strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (clean_username,)
            ).fetchone()
            if not row:
                raise ValueError("用户名或密码错误")

            if not row["is_active"]:
                raise ValueError("该账号已被冻结，请联系管理员")

            if not verify_password(password, row["password_hash"], row["salt"]):
                raise ValueError("用户名或密码错误")

            now_str = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (now_str, row["id"]),
            )
            conn.commit()

        is_admin_flag = bool(row["is_admin"]) if "is_admin" in row.keys() else False

        return UserResponse(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"] or "",
            created_at=row["created_at"],
            last_login_at=now_str,
            is_active=bool(row["is_active"]),
            is_admin=is_admin_flag,
        )

    def get_user_by_id(self, user_id: int) -> Optional[UserResponse]:
        """根据 ID 查询用户信息"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                return None
            is_admin_flag = bool(row["is_admin"]) if "is_admin" in row.keys() else False
            return UserResponse(
                id=row["id"],
                username=row["username"],
                display_name=row["display_name"] or "",
                created_at=row["created_at"],
                last_login_at=row["last_login_at"],
                is_active=bool(row["is_active"]),
                is_admin=is_admin_flag,
            )

    def issue_token_for_user(self, user: UserResponse, expires_in_seconds: int = 7 * 86400) -> str:
        """为用户签发有效期为 N 秒的 JWT Token"""
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.id),
            "username": user.username,
            "iat": int(now.timestamp()),
            "exp": int(now.timestamp()) + expires_in_seconds,
        }
        return create_jwt_token(payload, secret_key=self.secret_key)

    def authenticate_token(self, token: str) -> UserResponse:
        """校验 Token 并返回对应的用户实体"""
        payload = decode_jwt_token(token, secret_key=self.secret_key)
        sub = payload.get("sub")
        if not sub:
            raise ValueError("Token 中缺少用户标识")
        try:
            user_id = int(sub)
        except ValueError:
            raise ValueError("Token 中用户标识格式非法")

        user = self.get_user_by_id(user_id)
        if not user:
            raise ValueError("Token 对应的用户不存在或已被注销")
        if not user.is_active:
            raise ValueError("该账号已被冻结")
        return user

    def change_password(self, user_id: int, old_password: str, new_password: str) -> None:
        """修改用户登录密码"""
        if not new_password or len(new_password) < 5:
            raise ValueError("新密码长度至少需要 5 位")

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise ValueError("用户不存在")

            if not verify_password(old_password, row["password_hash"], row["salt"]):
                raise ValueError("原密码输入错误，请重新确认")

            new_pwd_hash, new_salt = hash_password(new_password)
            conn.execute(
                "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
                (new_pwd_hash, new_salt, user_id),
            )
            conn.commit()

    def list_all_users_with_stats(self) -> List[Dict[str, Any]]:
        """系统管理员浏览所有注册用户及其历史记录统计"""
        with self._connect() as conn:
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='extraction_history'"
            ).fetchone()
            if table_check:
                sql = """
                    SELECT 
                        u.id,
                        u.username,
                        u.display_name,
                        u.created_at,
                        u.last_login_at,
                        u.is_active,
                        u.is_admin,
                        (SELECT COUNT(*) FROM extraction_history h WHERE h.user_id = u.id) as history_count
                    FROM users u
                    ORDER BY u.id ASC
                """
            else:
                sql = """
                    SELECT 
                        u.id,
                        u.username,
                        u.display_name,
                        u.created_at,
                        u.last_login_at,
                        u.is_active,
                        u.is_admin,
                        0 as history_count
                    FROM users u
                    ORDER BY u.id ASC
                """
            rows = conn.execute(sql).fetchall()
            result = []
            for r in rows:
                result.append({
                    "id": r["id"],
                    "username": r["username"],
                    "display_name": r["display_name"] or "",
                    "created_at": r["created_at"],
                    "last_login_at": r["last_login_at"],
                    "is_active": bool(r["is_active"]),
                    "is_admin": bool(r["is_admin"]),
                    "history_count": int(r["history_count"] or 0),
                })
            return result
