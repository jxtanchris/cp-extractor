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

    # 测试超长截断 JSON (模拟 Unterminated string starting at line ...) 的自动抢救恢复能力
    truncated_output = """{
  "items": [
    {
      "characteristic_name": "螺纹大径公差",
      "characteristic_category": "尺寸",
      "specification": "6g",
      "inspection_method": "螺纹通止规",
      "acceptance_criteria": "通规通，止规止",
      "standard_clause": "4.1",
      "source_quote": "外螺纹大径公差带为 6g",
      "confidence": "高",
      "needs_human_review": false
    },
    {
      "characteristic_name": "被截断的半条数据",
      "characteristic_category": "尺寸",
      "specification": "±0.1"""
    salvaged_items = DeepSeekClient._parse_items_from_text(truncated_output)
    assert len(salvaged_items) == 1
    assert salvaged_items[0]["characteristic_name"] == "螺纹大径公差"


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


def test_sqlite_lexicon_crud_and_validator_integration():
    """测试 SQLite 量检具/单位/判定准则词库 CRUD 接口及 QualityGateValidator 动态生效"""
    from src.cp_extractor.schemas.models import (
        CheckItem,
        CharacteristicCategory,
        ConfidenceLevel,
        ExtractionPackage,
        StandardInfo,
    )
    from src.cp_extractor.core.validator import QualityGateValidator

    try:
        # 1. 查询初始 SQLite 词库（含多语言扩展统计）
        get_resp = client.get("/api/lexicon")
        assert get_resp.status_code == 200
        payload = get_resp.json()
        assert len(payload["categories"]) == 3
        assert payload["total_count"] > 30
        assert payload["total_expanded_terms"] > payload["total_count"]

        # 2. 批量新增带多语言同义词（aliases）的自定义量检具词条
        add_resp = client.post(
            "/api/lexicon",
            json={
                "category": "method",
                "keyword": "激光跟踪仪",
                "lang": "zh",
                "aliases": "Laser tracker, レーザートラッカー, Lasertracker",
                "description": "高精度空间与无损测量设备",
            },
        )
        assert add_resp.status_code == 200
        added = add_resp.json()["added"]
        assert len(added) == 1
        laser_id = added[0]["id"]
        assert "Laser tracker" in added[0]["alias_list"]
        assert "レーザートラッカー" in added[0]["alias_list"]

        # 3. 验证 QualityGateValidator 实时读取 SQLite 多语言同义词（如日文 'レーザートラッカー'）并拦截方法/判据混淆
        validator = QualityGateValidator()
        misused_item = CheckItem(
            characteristic_name="大型车身骨架尺寸",
            characteristic_category=CharacteristicCategory.DIMENSION,
            specification="1500 ± 0.5 mm",
            inspection_method="待定",
            acceptance_criteria="レーザートラッカーにて測定のこと",  # 误将日文量检具同义词写入判定准则
            standard_clause="3.1",
            source_quote="大型车身骨架尺寸 1500 ± 0.5 mm，レーザートラッカーにて測定のこと",
            confidence=ConfidenceLevel.HIGH,
            needs_human_review=False,
        )
        pkg = ExtractionPackage(
            standard_info=StandardInfo(file_name="body_frame.pdf", standard_category="公差/尺寸标准"),
            check_items=[misused_item],
        )
        validated_pkg = validator.validate_package(pkg)
        assert len(validated_pkg.pending_review_items) == 1
        assert any("レーザートラッカー" in msg for msg in validated_pkg.validation_report.method_misclassified_as_criteria)

        # 4. 修改词条多语言对照与说明
        upd_resp = client.put(
            f"/api/lexicon/{laser_id}",
            json={
                "keyword": "激光跟踪仪",
                "lang": "zh",
                "aliases": "Laser tracker, レーザートラッカー",
                "description": "更新后的设备说明",
            },
        )
        assert upd_resp.status_code == 200
        assert upd_resp.json()["updated"]["description"] == "更新后的设备说明"

        # 5. 删除词条
        del_resp = client.delete(f"/api/lexicon/{laser_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted_id"] == laser_id
    finally:
        # 6. 恢复默认词库
        reset_resp = client.post("/api/lexicon/reset", json={"category": "all"})
        assert reset_resp.status_code == 200
