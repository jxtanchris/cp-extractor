"""
用户认证与权限保护自动化测试套件 (Phase 1)
测试覆盖：
1. 用户注册（格式校验、重复用户名拦截、密码加盐存储）
2. 用户登录与 RFC 7519 JWT Token 签发与过期/篡改防御
3. 身份验证接口 /api/auth/me 与登出 /api/auth/logout
4. 核心受保护业务路由的 401 拦截校验（未登录禁止上传与提取）
"""

import time
from fastapi.testclient import TestClient
import pytest

from src.cp_extractor.api.app import app
from src.cp_extractor.core.auth import (
    AuthManager,
    create_jwt_token,
    decode_jwt_token,
    hash_password,
    verify_password,
)

client = TestClient(app)


def test_password_hashing_and_verification():
    """测试加盐哈希的唯一性与验证正确性"""
    pwd = "SecurePassword@123"
    h1, s1 = hash_password(pwd)
    h2, s2 = hash_password(pwd)

    # 随机盐不同，哈希值必然不同
    assert s1 != s2
    assert h1 != h2
    assert verify_password(pwd, h1, s1) is True
    assert verify_password(pwd, h2, s2) is True
    assert verify_password("WrongPassword", h1, s1) is False


def test_jwt_token_sign_verify_and_tamper_defense():
    """测试原生 JWT 签名、校验、过期时间与防篡改能力"""
    payload = {"sub": "1001", "username": "quality_eng", "exp": int(time.time()) + 3600}
    token = create_jwt_token(payload, secret_key="secret-key-1")

    decoded = decode_jwt_token(token, secret_key="secret-key-1")
    assert decoded["sub"] == "1001"
    assert decoded["username"] == "quality_eng"

    # 1. 错误密钥解密
    with pytest.raises(ValueError, match="Token 签名验证失败"):
        decode_jwt_token(token, secret_key="wrong-secret-key")

    # 2. 篡改载荷
    parts = token.split(".")
    tampered_token = f"{parts[0]}.eyJhZG1pbiI6dHJ1ZX0.{parts[2]}"
    with pytest.raises(ValueError, match="Token 签名验证失败"):
        decode_jwt_token(tampered_token, secret_key="secret-key-1")

    # 3. 过期 Token 校验
    expired_payload = {"sub": "1001", "exp": int(time.time()) - 10}
    expired_token = create_jwt_token(expired_payload, secret_key="secret-key-1")
    with pytest.raises(ValueError, match="Token 已过期"):
        decode_jwt_token(expired_token, secret_key="secret-key-1")


def test_user_registration_and_validation():
    """测试用户注册接口与格式校验"""
    unique_username = f"user_reg_{int(time.time() * 1000)}"

    # 1. 正常注册
    resp = client.post(
        "/api/auth/register",
        json={
            "username": unique_username,
            "password": "mypassword123",
            "display_name": "质量部李工",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["user"]["username"] == unique_username
    assert data["user"]["display_name"] == "质量部李工"
    assert "password" not in data["user"]

    # 2. 重复用户名注册拦截
    dup_resp = client.post(
        "/api/auth/register",
        json={"username": unique_username, "password": "mypassword123"},
    )
    assert dup_resp.status_code == 400
    assert "已被注册" in dup_resp.json()["detail"]

    # 3. 密码过短 (<6位)
    short_pwd_resp = client.post(
        "/api/auth/register",
        json={"username": f"user_short_{int(time.time())}", "password": "123"},
    )
    assert short_pwd_resp.status_code == 422  # Pydantic 字段验证失败

    # 4. 用户名过短 (<3位)
    short_user_resp = client.post(
        "/api/auth/register",
        json={"username": "ab", "password": "password123"},
    )
    assert short_user_resp.status_code == 422


def test_user_login_and_me_endpoints():
    """测试登录鉴权、Token 获取与当前身份查询"""
    uname = f"user_login_{int(time.time() * 1000)}"
    pwd = "LoginPass@2026"

    # 先注册
    client.post("/api/auth/register", json={"username": uname, "password": pwd, "display_name": "张工"})

    # 1. 错误密码登录拦截
    bad_login = client.post("/api/auth/login", json={"username": uname, "password": "wrongpassword"})
    assert bad_login.status_code == 401
    assert "用户名或密码错误" in bad_login.json()["detail"]

    # 2. 不存在用户登录拦截
    no_user = client.post("/api/auth/login", json={"username": "non_existent_user_999", "password": pwd})
    assert no_user.status_code == 401

    # 3. 正确登录并获取 Token
    login_resp = client.post("/api/auth/login", json={"username": uname, "password": pwd})
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    assert token

    headers = {"Authorization": f"Bearer {token}"}

    # 4. 获取个人信息
    me_resp = client.get("/api/auth/me", headers=headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["username"] == uname
    assert me_resp.json()["display_name"] == "张工"

    # 5. 退出登录
    logout_resp = client.post("/api/auth/logout", headers=headers)
    assert logout_resp.status_code == 200
    assert logout_resp.json()["logged_out"] is True


def test_unauthenticated_requests_blocked_with_401():
    """
    红线安全测试：
    验证必须登录后才能使用上传与提取功能；
    未携带有效 Bearer Token 调用 /api/upload, /api/extract, /api/extract-document 必须被 401 强行拦截。
    """
    # 1. 未登录调用 /api/upload
    up_resp = client.post(
        "/api/upload",
        files={"file": ("test.txt", b"Standard text", "text/plain")},
    )
    assert up_resp.status_code == 401
    assert "必须登录" in up_resp.json()["detail"]

    # 2. 未登录调用 /api/extract
    ext_resp = client.post(
        "/api/extract",
        json={"file_name": "test.md", "content": "1.1 尺寸 10 ± 0.1 mm"},
    )
    assert ext_resp.status_code == 401
    assert "必须登录" in ext_resp.json()["detail"]

    # 3. 未登录调用 /api/extract-document
    ext_doc_resp = client.post(
        "/api/extract-document",
        files={"file": ("test.txt", b"Standard text", "text/plain")},
    )
    assert ext_doc_resp.status_code == 401
    assert "必须登录" in ext_doc_resp.json()["detail"]

    # 4. 未登录调用 /api/approve-review
    rev_resp = client.post(
        "/api/approve-review",
        json={"package": {}, "review_index": 0, "action": "approve"},
    )
    assert rev_resp.status_code == 401
    assert "必须登录" in rev_resp.json()["detail"]

    # 5. 伪造或损坏的 Token 拦截
    fake_header = {"Authorization": "Bearer invalid.fake.token"}
    fake_resp = client.post(
        "/api/extract",
        json={"file_name": "test.md", "content": "1.1 尺寸 10 ± 0.1 mm"},
        headers=fake_header,
    )
    assert fake_resp.status_code == 401

    # 6. 注册并登录后，成功通过鉴权访问
    valid_uname = f"authed_user_{int(time.time() * 1000)}"
    reg_resp = client.post(
        "/api/auth/register",
        json={"username": valid_uname, "password": "password123"},
    )
    valid_token = reg_resp.json()["access_token"]
    valid_headers = {"Authorization": f"Bearer {valid_token}"}

    auth_ext_resp = client.post(
        "/api/extract",
        json={
            "file_name": "test.md",
            "content": "1.1 尺寸 10 ± 0.1 mm",
            "engine_mode": "rule",
        },
        headers=valid_headers,
    )
    # 鉴权通过（返回 200）
    assert auth_ext_resp.status_code == 200
