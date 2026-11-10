"""
提取历史记录多租户隔离与防越权测试 (test_history.py)
"""

import time
from fastapi.testclient import TestClient
from src.cp_extractor.api.app import app
from src.cp_extractor.core.history import HistoryManager

client = TestClient(app)


def _register_user(prefix: str):
    ts = int(time.time() * 1000)
    username = f"{prefix}_{ts}"
    resp = client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": f"工程师_{prefix}"},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    user = resp.json()["user"]
    return user, {"Authorization": f"Bearer {token}"}


def test_history_manager_core_crud(tmp_path):
    """测试 HistoryManager 底层数据库 CRUD"""
    db_file = tmp_path / "test_history.db"
    mgr = HistoryManager(db_path=db_file)

    # 1. 创建记录
    rec = mgr.record_extraction(
        user_id=101,
        file_name="gb_standard_sample.pdf",
        standard_category="公差/尺寸标准",
        mode="full",
        engine_used="DeepSeek API (deepseek-chat)",
        valid_count=10,
        pending_count=2,
        excel_url="/api/download/gb_sample.xlsx",
        json_url="/api/download/gb_sample.json",
        source_file_url="/api/source/gb_sample.pdf",
    )
    assert rec["id"] > 0
    assert rec["user_id"] == 101
    assert rec["file_name"] == "gb_standard_sample.pdf"

    # 2. 查询列表与计数
    assert mgr.count_history(user_id=101) == 1
    assert mgr.count_history(user_id=102) == 0

    list_user = mgr.list_history(user_id=101)
    assert len(list_user) == 1
    assert list_user[0]["id"] == rec["id"]

    # 3. 越权保护
    import pytest
    with pytest.raises(PermissionError):
        mgr.get_record(rec["id"], user_id=102, is_admin=False)

    # 4. 删除
    assert mgr.delete_record(rec["id"], user_id=101) is True
    assert mgr.count_history(user_id=101) == 0


def test_api_history_isolation_and_idor_protection():
    """
    测试历史记录 API 端点：
    1. 用户 A 提取产生记录，仅用户 A 可见；
    2. 用户 B 查看自身历史为空，越权查看或删除用户 A 记录触发 403 拦截；
    3. 管理员账号可审计查看所有用户历史；
    4. 一键重新载入历史包成功返回。
    """
    user_a, headers_a = _register_user("hist_user_a")
    user_b, headers_b = _register_user("hist_user_b")

    admin_login = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    sample_data = client.get("/api/samples/sample_tolerance_iso2768.md").json()

    # 1. 用户 A 运行提取
    ext_resp = client.post(
        "/api/extract",
        json={
            "file_name": "sample_tolerance_iso2768.md",
            "content": sample_data["content"],
            "engine_mode": "rule",
        },
        headers=headers_a,
    )
    assert ext_resp.status_code == 200

    # 2. 用户 A 查看自己的历史记录
    hist_a_resp = client.get("/api/history", headers=headers_a)
    assert hist_a_resp.status_code == 200
    a_data = hist_a_resp.json()
    assert a_data["total"] >= 1
    rec_a = next(r for r in a_data["records"] if r["file_name"] == "sample_tolerance_iso2768.md")
    assert rec_a["user_id"] == user_a["id"]
    assert rec_a["valid_count"] == 10
    record_id = rec_a["id"]

    # 3. 用户 B 查看自己的历史记录 -> 隔离保护，看不到用户 A 的记录
    hist_b_resp = client.get("/api/history", headers=headers_b)
    assert hist_b_resp.status_code == 200
    b_records = [r for r in hist_b_resp.json()["records"] if r["id"] == record_id]
    assert len(b_records) == 0

    # 4. 用户 B 尝试越权调用用户 A 的 reload 接口 -> 403 Forbidden 强行阻断
    idor_reload = client.get(f"/api/history/{record_id}/reload", headers=headers_b)
    assert idor_reload.status_code == 403
    assert "越权" in idor_reload.json()["detail"]

    # 5. 用户 B 尝试越权删除用户 A 的历史记录 -> 403 Forbidden 强行阻断
    idor_del = client.delete(f"/api/history/{record_id}", headers=headers_b)
    assert idor_del.status_code == 403

    # 6. 用户 A 正常载入自己的历史结果 -> 200 OK，完整还原 package
    reload_resp = client.get(f"/api/history/{record_id}/reload", headers=headers_a)
    assert reload_resp.status_code == 200
    r_body = reload_resp.json()
    assert "package" in r_body
    assert len(r_body["package"]["check_items"]) == 10
    assert r_body["record"]["id"] == record_id

    # 7. 管理员审计查看 -> 可在列表中看到该记录
    admin_hist = client.get("/api/history", headers=admin_headers)
    assert admin_hist.status_code == 200
    admin_rec_ids = [r["id"] for r in admin_hist.json()["records"]]
    assert record_id in admin_rec_ids

    # 8. 用户 A 删除自身记录 -> 200 OK
    del_resp = client.delete(f"/api/history/{record_id}", headers=headers_a)
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True
