"""
FastAPI 接口与 DeepSeek 客户端单元测试
"""

from fastapi.testclient import TestClient
from src.cp_extractor.api.app import app
from src.cp_extractor.core.llm_client import DeepSeekClient

client = TestClient(app)


def test_web_ui_and_samples_endpoints():
    # 1. 首页加载
    resp = client.get("/")
    assert resp.status_code == 200
    assert "CP-Extractor" in resp.text

    # 2. 样本列表
    s_resp = client.get("/api/samples")
    assert s_resp.status_code == 200
    samples = s_resp.json()
    assert len(samples) == 4

    # 3. 单个样本加载
    detail_resp = client.get("/api/samples/sample_tolerance_iso2768.md")
    assert detail_resp.status_code == 200
    assert "ISO 2768" in detail_resp.json()["content"]


def test_extract_and_human_review_workflow():
    sample_data = client.get("/api/samples/sample_tolerance_iso2768.md").json()

    # 1. 调用提取接口
    ext_resp = client.post(
        "/api/extract",
        json={
            "file_name": "sample_tolerance_iso2768.md",
            "content": sample_data["content"],
            "engine_mode": "rule",
        },
    )
    assert ext_resp.status_code == 200
    body = ext_resp.json()
    pkg = body["package"]
    assert len(pkg["check_items"]) == 10
    assert len(pkg["pending_review_items"]) == 1

    # 2. 质量工程师在 Web 界面人工审核并准入待确认项
    pending_item = pkg["pending_review_items"][0]
    pending_item["specification"] = "轮廓度 ≤ 0.5 mm"
    pending_item["inspection_method"] = "三坐标测量机"
    pending_item["acceptance_criteria"] = "轮廓度误差 ≤ 0.5 mm"

    rev_resp = client.post(
        "/api/approve-review",
        json={
            "package": pkg,
            "review_index": 0,
            "action": "approve",
            "updated_item": pending_item,
        },
    )
    assert rev_resp.status_code == 200
    updated_pkg = rev_resp.json()["package"]
    assert len(updated_pkg["check_items"]) == 11
    assert len(updated_pkg["pending_review_items"]) == 0

    # 3. 验证下载接口
    dl_resp = client.get(rev_resp.json()["excel_download_url"])
    assert dl_resp.status_code == 200


def test_deepseek_json_parser_compatibility():
    """测试 DeepSeek-V3 (JSON 对象) 与 DeepSeek-R1 (代码块包裹) 的响应解析器"""
    raw_r1_output = """
```json
{
  "items": [
    {
      "characteristic_name": "抗拉强度",
      "characteristic_category": "力学性能",
      "specification": "≥ 480 MPa",
      "inspection_method": "拉伸试验机",
      "acceptance_criteria": "Rm ≥ 480 MPa",
      "standard_clause": "3.1",
      "source_quote": "抗拉强度 Rm 应在 480 ~ 620 MPa 范围内",
      "confidence": "high",
      "needs_human_review": false
    }
  ]
}
```
"""
    items = DeepSeekClient._parse_items_from_text(raw_r1_output)
    assert len(items) == 1
    assert items[0]["characteristic_category"] == "材料"
    assert items[0]["confidence"] == "高"


def test_template_management_endpoints():
    """测试提示词模板查询、修改保存与恢复出厂默认接口"""
    # 1. 获取所有模板
    list_resp = client.get("/api/templates")
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert len(data["templates"]) == 5
    base_tpl = next(t for t in data["templates"] if t["key"] == "base")
    original_content = base_tpl["content"]

    try:
        # 2. 修改 base 模板并验证已标记 is_modified
        custom_text = original_content + "\n5. 额外自定义人工确认规则测试。"
        put_resp = client.put("/api/templates/base", json={"content": custom_text})
        assert put_resp.status_code == 200
        assert put_resp.json()["is_modified"] is True
        assert "额外自定义人工确认规则测试" in put_resp.json()["content"]

        # 3. 恢复默认并验证
        reset_resp = client.post("/api/templates/base/reset")
        assert reset_resp.status_code == 200
        assert reset_resp.json()["is_modified"] is False
    finally:
        # 确保测试后模板保持原始状态
        client.put("/api/templates/base", json={"content": original_content})
