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


def test_default_admin_login_and_api_key_permission(tmp_path, monkeypatch):
    """
    测试默认管理员账号与 API Key 修改权限拦截：
    1. 默认管理员账号密码 admin/admin 开箱即用
    2. 普通用户尝试保存 API Key 到服务端环境被 403 严格拦截
    3. 仅管理员账号允许调用 /api/llm/save-env 修改服务端 API Key
    """
    # 1. 默认 admin/admin 登录
    admin_login = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert admin_login.status_code == 200, f"admin 登录失败: {admin_login.text}"
    admin_data = admin_login.json()
    admin_token = admin_data["access_token"]
    assert admin_data["user"]["username"] == "admin"
    assert admin_data["user"]["is_admin"] is True

    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # 2. 普通用户注册与尝试修改 API Key
    normal_uname = f"engineer_{int(time.time() * 1000)}"
    reg_normal = client.post("/api/auth/register", json={"username": normal_uname, "password": "password123"})
    normal_token = reg_normal.json()["access_token"]
    assert reg_normal.json()["user"]["is_admin"] is False
    normal_headers = {"Authorization": f"Bearer {normal_token}"}

    key_payload = {
        "provider": "deepseek",
        "api_key": "sk-new-admin-test-key",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "temperature": 0.0,
    }

    # 未登录访问修改 API Key -> 401
    unauth_resp = client.post("/api/llm/save-env", json=key_payload)
    assert unauth_resp.status_code == 401

    # 普通用户修改 API Key -> 403 Forbidden
    forbidden_resp = client.post("/api/llm/save-env", json=key_payload, headers=normal_headers)
    assert forbidden_resp.status_code == 403
    assert "仅系统管理员" in forbidden_resp.json()["detail"]

    # 3. 管理员成功修改 API Key
    # 使用临时文件测试写入避免污染生产环境 .env
    fake_env = tmp_path / ".env"
    fake_env.write_text("DEEPSEEK_API_KEY=sk-init\n", encoding="utf-8")
    import sys
    app_module = sys.modules["src.cp_extractor.api.app"]
    monkeypatch.setattr(app_module, "ROOT_DIR", tmp_path)

    admin_save_resp = client.post("/api/llm/save-env", json=key_payload, headers=admin_headers)
    assert admin_save_resp.status_code == 200
    assert admin_save_resp.json()["saved"] is True
    assert "DEEPSEEK_API_KEY=sk-new-admin-test-key" in fake_env.read_text(encoding="utf-8")


def test_user_prompt_template_isolation_and_fallback():
    """
    Phase 2 专项测试：用户提示词两级继承与隔离机制 (System Default + User Override)
    1. 用户 A 修改公共基础模板，只对用户 A 生效
    2. 用户 B 获取的仍是系统官方默认模板，未受用户 A 影响
    3. 磁盘文件 templates/base_template.md 保持原始干净，不被用户微调篡改
    4. 用户 A 在组装/预览提示词时自动应用个人定制版本；用户 B 应用官方版本
    5. 用户 A 执行 reset 恢复默认后，清除用户级覆盖并回退至系统官方版本
    """
    ts = int(time.time() * 1000)
    user_a = f"engineer_a_{ts}"
    user_b = f"engineer_b_{ts}"

    token_a = client.post("/api/auth/register", json={"username": user_a, "password": "password123"}).json()["access_token"]
    token_b = client.post("/api/auth/register", json={"username": user_b, "password": "password123"}).json()["access_token"]

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 1. 初始状态：用户 A 与 用户 B 查看 base 模板，均为官方默认且未定制
    list_a = client.get("/api/templates", headers=headers_a).json()["templates"]
    list_b = client.get("/api/templates", headers=headers_b).json()["templates"]
    base_a = next(t for t in list_a if t["key"] == "base")
    base_b = next(t for t in list_b if t["key"] == "base")
    assert base_a["content"] == base_b["content"]
    assert base_a["is_user_custom"] is False
    assert base_b["is_user_custom"] is False
    original_official_content = base_a["content"]

    # 2. 用户 A 定制修改 base 模板
    custom_content_a = original_official_content + "\n\n【工程师 A 专属准入准则】必须加测抗疲劳极限指标！"
    put_a = client.put("/api/templates/base", json={"content": custom_content_a}, headers=headers_a)
    assert put_a.status_code == 200
    assert put_a.json()["is_user_custom"] is True
    assert put_a.json()["is_modified"] is True

    # 3. 隔离性验证：
    # 用户 A 再次查询，显示个性化定制内容
    a_new_list = client.get("/api/templates", headers=headers_a).json()["templates"]
    a_new_base = next(t for t in a_new_list if t["key"] == "base")
    assert "工程师 A 专属准入准则" in a_new_base["content"]
    assert a_new_base["is_user_custom"] is True

    # 用户 B 查询，依然是官方默认内容！不受任何污染！
    b_new_list = client.get("/api/templates", headers=headers_b).json()["templates"]
    b_new_base = next(t for t in b_new_list if t["key"] == "base")
    assert "工程师 A 专属准入准则" not in b_new_base["content"]
    assert b_new_base["content"] == original_official_content
    assert b_new_base["is_user_custom"] is False

    # 4. 预览提示词自动隔离验证
    preview_a = client.post(
        "/api/preview-prompt",
        json={"content": "3.1 尺寸 20 ± 0.2 mm", "manual_category": "tolerance"},
        headers=headers_a,
    ).json()["prompt"]
    preview_b = client.post(
        "/api/preview-prompt",
        json={"content": "3.1 尺寸 20 ± 0.2 mm", "manual_category": "tolerance"},
        headers=headers_b,
    ).json()["prompt"]
    assert "工程师 A 专属准入准则" in preview_a
    assert "工程师 A 专属准入准则" not in preview_b

    # 5. 用户 A 恢复出厂设置
    reset_a = client.post("/api/templates/base/reset", headers=headers_a)
    assert reset_a.status_code == 200
    assert reset_a.json()["is_user_custom"] is False
    assert "工程师 A 专属准入准则" not in reset_a.json()["content"]

    # 6. 恢复后，用户 A 再次查询，已彻底回退至官方默认
    a_reset_list = client.get("/api/templates", headers=headers_a).json()["templates"]
    a_reset_base = next(t for t in a_reset_list if t["key"] == "base")
    assert a_reset_base["content"] == original_official_content
    assert a_reset_base["is_user_custom"] is False


def test_storage_sandbox_and_idor_prevention(tmp_path):
    """
    Phase 3 专项测试：文件存储沙箱隔离与防越权 (IDOR) 保护机制
    1. 用户 A 上传与导出文件物理隔离在 output/users/{user_a_id}/ 目录下
    2. 用户 A 携带 Header 或 URL ?token=... 均可正常预览与下载自身文件
    3. 用户 B 尝试访问用户 A 的文件被 403 严格拦截 (防水平越权 IDOR)
    4. 未登录访问被 401 拦截
    5. 管理员 (admin) 拥有跨用户审计特权，可正常读取
    6. 路径遍历注入攻击 (Path Traversal, ../) 被 400 严格拦截
    """
    ts = int(time.time() * 1000)
    user_a = f"engineer_idor_a_{ts}"
    user_b = f"engineer_idor_b_{ts}"

    token_a = client.post("/api/auth/register", json={"username": user_a, "password": "password123"}).json()["access_token"]
    token_b = client.post("/api/auth/register", json={"username": user_b, "password": "password123"}).json()["access_token"]
    admin_token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["access_token"]

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    headers_admin = {"Authorization": f"Bearer {admin_token}"}

    # 1. 用户 A 上传文档
    upload_resp = client.post(
        "/api/upload",
        files={"file": ("idor_spec_a.txt", b"1.1 Characteristic Tolerance 10 +/- 0.1 mm\n", "text/plain")},
        headers=headers_a,
    )
    assert upload_resp.status_code == 200
    source_filename = upload_resp.json()["source_file_url"].split("/")[-1]

    # 2. 用户 A 提取并生成导出产物
    ext_resp = client.post(
        "/api/extract",
        json={
            "file_name": source_filename,
            "content": "1.1 Characteristic Tolerance 10 +/- 0.1 mm",
            "engine_mode": "rule",
        },
        headers=headers_a,
    )
    assert ext_resp.status_code == 200
    excel_url = ext_resp.json()["excel_download_url"]
    excel_filename = excel_url.split("/")[-1]

    # 3. 用户 A 正常访问自身文件（Header 模式与 URL ?token= 模式）
    res_a_view = client.get(f"/api/source/{source_filename}", headers=headers_a)
    assert res_a_view.status_code == 200

    res_a_view_query = client.get(f"/api/source/{source_filename}?token={token_a}")
    assert res_a_view_query.status_code == 200

    res_a_dl = client.get(f"/api/download/{excel_filename}", headers=headers_a)
    assert res_a_dl.status_code == 200

    # 4. 未登录访问 -> 401 Unauthorized
    unauth_view = client.get(f"/api/source/{source_filename}")
    assert unauth_view.status_code == 401

    unauth_dl = client.get(f"/api/download/{excel_filename}")
    assert unauth_dl.status_code == 401

    # 5. 用户 B 越权访问用户 A 的文件 -> 403 Forbidden 严格拦截
    idor_view = client.get(f"/api/source/{source_filename}", headers=headers_b)
    assert idor_view.status_code == 403
    assert "越权访问拦截" in idor_view.json()["detail"]

    idor_dl = client.get(f"/api/download/{excel_filename}", headers=headers_b)
    assert idor_dl.status_code == 403
    assert "越权访问拦截" in idor_dl.json()["detail"]

    idor_view_query = client.get(f"/api/source/{source_filename}?token={token_b}")
    assert idor_view_query.status_code == 403

    # 6. 管理员特权审计访问 -> 200 OK
    admin_view = client.get(f"/api/source/{source_filename}", headers=headers_admin)
    assert admin_view.status_code == 200

    admin_dl = client.get(f"/api/download/{excel_filename}", headers=headers_admin)
    assert admin_dl.status_code == 200

    # 7. 路径遍历与非法文件名攻击检测 -> 400 Bad Request
    from src.cp_extractor.api.app import _locate_user_file
    from src.cp_extractor.schemas.auth import UserResponse
    from fastapi import HTTPException
    import pytest

    mock_user = UserResponse(id=1, username="test", display_name="Test", is_admin=False, created_at="2026-01-01T00:00:00")
    with pytest.raises(HTTPException) as exc_info:
        _locate_user_file("../../etc/passwd", mock_user)  # 包含路径穿越字符
    assert exc_info.value.status_code == 400
    assert "非法" in exc_info.value.detail


def test_feedback_rule_learning_user_isolation():
    """
    Phase 3 专项测试：自学习规则引擎的多租户/多用户经验隔离机制
    1. 用户 A 手工准入特定项满 3 次，晋升为用户 A 的生效规则
    2. 用户 A 再次提取时，系统自动放行并应用专属方法与判据
    3. 用户 B 提取同一标准文档时，依然保持待审核状态，不受用户 A 的自学习规则影响
    """
    ts = int(time.time() * 1000)
    user_a = f"engineer_learn_a_{ts}"
    user_b = f"engineer_learn_b_{ts}"

    token_a = client.post("/api/auth/register", json={"username": user_a, "password": "password123"}).json()["access_token"]
    token_b = client.post("/api/auth/register", json={"username": user_b, "password": "password123"}).json()["access_token"]

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 重置两者的学习规则
    client.post("/api/learning/reset", headers=headers_a)
    client.post("/api/learning/reset", headers=headers_b)
    client.put("/api/learning/settings", json={"threshold": 3}, headers=headers_a)

    sample_data = client.get("/api/samples/sample_tolerance_iso2768.md").json()

    try:
        # 1. 用户 A 提取公差样本（10 条正式，1 条待确认）
        ext_a = client.post(
            "/api/extract",
            json={"file_name": "sample_tolerance_iso2768.md", "content": sample_data["content"], "engine_mode": "rule"},
            headers=headers_a,
        ).json()["package"]
        assert len(ext_a["pending_review_items"]) == 1
        pending_item = ext_a["pending_review_items"][0]

        revised_item = {
            **pending_item,
            "specification": "轮廓度 ⌓ ≤ 0.5 mm",
            "inspection_method": "工程师A专属影像仪",
            "acceptance_criteria": "实测轮廓度误差 ≤ 0.5 mm",
        }

        # 用户 A 连续 3 次准入
        for i in range(3):
            r = client.post(
                "/api/approve-review",
                json={"package": ext_a, "review_index": 0, "action": "approve", "updated_item": revised_item},
                headers=headers_a,
            ).json()["learning_event"]

        assert r["just_promoted"] is True
        assert r["rule"]["is_active"] is True

        # 2. 用户 A 再次提取：自学习规则生效，待确认项自动放行为 0，正式项增加到 11
        ext_a_again = client.post(
            "/api/extract",
            json={"file_name": "sample_tolerance_iso2768.md", "content": sample_data["content"], "engine_mode": "rule"},
            headers=headers_a,
        ).json()["package"]
        assert len(ext_a_again["pending_review_items"]) == 0
        assert len(ext_a_again["check_items"]) == 11
        item_a = next(i for i in ext_a_again["check_items"] if i["characteristic_name"] == "局部非关键翻边轮廓度")
        assert item_a["inspection_method"] == "工程师A专属影像仪"

        # 3. 用户 B 提取相同的公差样本：隔离保护！用户 B 依然有 1 条待人工确认项，绝不被用户 A 的规则误放行！
        ext_b = client.post(
            "/api/extract",
            json={"file_name": "sample_tolerance_iso2768.md", "content": sample_data["content"], "engine_mode": "rule"},
            headers=headers_b,
        ).json()["package"]
        assert len(ext_b["pending_review_items"]) == 1
        assert len(ext_b["check_items"]) == 10
        assert ext_b["pending_review_items"][0]["characteristic_name"] == "局部非关键翻边轮廓度"
    finally:
        client.post("/api/learning/reset", headers=headers_a)
        client.post("/api/learning/reset", headers=headers_b)


def test_frontend_auth_elements_present():
    """
    Phase 4 专项测试：前端 Web 页面静态元素及认证交互结构验证
    1. 首页正常加载返回 200
    2. 包含用户登录状态徽章与登录/注册弹窗模态框 (authModal)
    3. 登录弹窗不显示系统管理员初始密码等提示（提升安全性与简洁度）
    4. 包含管理员特权提示与普通用户 Key 隐藏保护逻辑
    5. 包含提示词个人定制标识逻辑
    6. 包含修改密码入口与修改密码模态框
    """
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text

    assert 'id="authModal"' in html
    assert 'id="authLoginForm"' in html
    assert 'id="authRegForm"' in html
    assert 'id="userBadge"' in html
    assert 'id="changePwdBtn"' in html
    assert 'id="changePasswordModal"' in html
    assert 'submitChangePassword' in html
    assert 'id="normalUserKeyNotice"' in html
    assert 'fetchWithAuth' in html
    assert 'appendTokenToUrl' in html
    assert '个人定制' in html
    # 验证登录模态框中无默认账号密码提示
    assert '系统管理员初始账号密码为' not in html
    # 验证系统管理员用户管理与历史审计模态框
    assert 'id="adminUsersModal"' in html
    assert 'id="adminUsersTableBody"' in html
    assert 'id="historyAdminUserSelect"' in html


def test_user_change_password_workflow():
    """测试已登录用户修改个人密码全流程与安全性"""
    # 1. 未登录访问修改密码接口 -> 401 拦截
    unauth_resp = client.post(
        "/api/auth/change-password",
        json={"old_password": "any", "new_password": "newpassword123"},
    )
    assert unauth_resp.status_code == 401

    # 2. 注册并登录专用测试账号
    uname = f"pwd_user_{int(time.time()*1000)}"
    reg_resp = client.post(
        "/api/auth/register",
        json={"username": uname, "password": "OriginalPwd123", "display_name": "密码测试工"},
    )
    assert reg_resp.status_code == 200
    token = reg_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 3. 原密码输入错误 -> 400 拦截
    wrong_old_resp = client.post(
        "/api/auth/change-password",
        json={"old_password": "WrongPassword!", "new_password": "ValidNewPassword888"},
        headers=headers,
    )
    assert wrong_old_resp.status_code == 400
    assert "原密码输入错误" in wrong_old_resp.json()["detail"]

    # 4. 新密码过短 (<5位) -> 422 验证失败
    short_pwd_resp = client.post(
        "/api/auth/change-password",
        json={"old_password": "OriginalPwd123", "new_password": "123"},
        headers=headers,
    )
    assert short_pwd_resp.status_code in (400, 422)

    # 5. 正确修改密码 -> 200 成功
    ok_resp = client.post(
        "/api/auth/change-password",
        json={"old_password": "OriginalPwd123", "new_password": "BrandNewSecret999"},
        headers=headers,
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["status"] == "success"

    # 6. 使用原密码再次登录 -> 401 失败
    old_login_resp = client.post(
        "/api/auth/login",
        json={"username": uname, "password": "OriginalPwd123"},
    )
    assert old_login_resp.status_code == 401

    # 7. 使用新密码登录 -> 200 成功
    new_login_resp = client.post(
        "/api/auth/login",
        json={"username": uname, "password": "BrandNewSecret999"},
    )
    assert new_login_resp.status_code == 200
    assert "access_token" in new_login_resp.json()


def test_logout_frontend_resets_workspace_state():
    """
    前端登出安全规范验证：
    1. 前端包含 resetWorkspaceState 状态复位函数；
    2. resetWorkspaceState 会清空 selectedFile、dropzone has-file 样式、恢复 dzTitle / dzIcon 初始文案与图标；
    3. 清空右侧 KPI 统计与表格占位；
    4. 在 handleLogout() 和 401 会话失效中被可靠触发。
    """
    html_resp = client.get("/")
    assert html_resp.status_code == 200
    html = html_resp.text

    # 1. 验证存在 resetWorkspaceState 定义
    assert "function resetWorkspaceState()" in html
    # 2. 验证清空文件选择与拖拽区状态
    assert "selectedFile = null" in html
    assert "dropzone.classList.remove('has-file')" in html
    assert "currentPackage = null" in html
    assert "currentParsedText = ''" in html
    # 3. 验证复位右侧 KPI 及表格提示
    assert "kpiCategory" in html
    assert "validTableBody" in html
    assert "pendingContainer" in html
    # 4. 验证登出及 401 中调用了 resetWorkspaceState，并且不存在 loadTemplateList 异常调用
    assert "resetWorkspaceState();" in html
    assert "loadTemplateList" not in html
    assert "headerViewTextBtn" in html
    assert "viewValid" in html





