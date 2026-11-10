"""
FastAPI 接口与 DeepSeek 客户端单元测试
"""

from fastapi.testclient import TestClient
from src.cp_extractor.api.app import app
from src.cp_extractor.core.llm_client import DeepSeekClient

client = TestClient(app)


def _get_auth_headers():
    reg_resp = client.post(
        "/api/auth/register",
        json={"username": "qa_tester_runner", "password": "password123", "display_name": "自动化测试员"},
    )
    if reg_resp.status_code == 200:
        token = reg_resp.json()["access_token"]
    else:
        login_resp = client.post(
            "/api/auth/login",
            json={"username": "qa_tester_runner", "password": "password123"},
        )
        token = login_resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


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
    headers = _get_auth_headers()
    sample_data = client.get("/api/samples/sample_tolerance_iso2768.md").json()

    # 1. 调用提取接口
    ext_resp = client.post(
        "/api/extract",
        json={
            "file_name": "sample_tolerance_iso2768.md",
            "content": sample_data["content"],
            "engine_mode": "rule",
        },
        headers=headers,
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
        headers=headers,
    )
    assert rev_resp.status_code == 200
    updated_pkg = rev_resp.json()["package"]
    assert len(updated_pkg["check_items"]) == 11
    assert len(updated_pkg["pending_review_items"]) == 0
    assert "learning_event" in rev_resp.json()

    # 3. 验证下载接口并清理测试产生的自学习计数
    dl_resp = client.get(rev_resp.json()["excel_download_url"], headers=headers)
    assert dl_resp.status_code == 200
    client.post("/api/learning/reset", headers=headers)


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
    # 1. 获取所有模板（基础模板 + 4 类插件 + char_list 仅特性清单模板，共 6 个）
    list_resp = client.get("/api/templates")
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert len(data["templates"]) == 6
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


def test_multi_provider_llm_switching_and_config(tmp_path):
    """测试多后端大模型厂商（DeepSeek / Qwen / GLM / Kimi / SiliconFlow / OpenAI / Ollama）切换、URL 规范化与 <think> 剥离"""
    from src.cp_extractor.core.llm_client import (
        LLMClient,
        LLM_PROVIDERS,
        normalize_base_url,
        persist_llm_config_to_env,
    )

    # 1. 验证 /api/llm/providers 与 /api/config 接口返回完整 8 类预设厂商
    cfg_resp = client.get("/api/llm/providers")
    assert cfg_resp.status_code == 200
    cfg_data = cfg_resp.json()
    provider_keys = {p["key"] for p in cfg_data["providers"]}
    assert provider_keys == set(LLM_PROVIDERS.keys())
    assert {"deepseek", "qwen", "zhipu", "kimi", "siliconflow", "openai", "ollama", "custom"}.issubset(provider_keys)

    # 2. 验证不同厂商 Base URL 的智能规范化（保留 /v4、/compatible-mode/v1，自动补齐 /v1）
    assert normalize_base_url("https://api.deepseek.com") == "https://api.deepseek.com/v1"
    assert normalize_base_url("https://open.bigmodel.cn/api/paas/v4") == "https://open.bigmodel.cn/api/paas/v4"
    assert normalize_base_url("https://dashscope.aliyuncs.com/compatible-mode/v1") == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert normalize_base_url("https://api.moonshot.cn/v1/chat/completions") == "https://api.moonshot.cn/v1"

    # 3. 验证本地 Ollama 免 API Key 自动就绪与各厂商客户端初始化
    ollama_client = LLMClient(provider="ollama", api_key="", base_url="http://127.0.0.1:11434/v1", model="qwen2.5:14b")
    assert ollama_client.is_configured is True
    assert ollama_client.display_label == "Ollama (qwen2.5:14b)"

    qwen_client = LLMClient(provider="qwen", api_key="sk-test-qwen", model="qwen-plus", temperature=0.2)
    assert qwen_client.is_configured is True
    assert qwen_client.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert qwen_client.display_label == "Qwen (qwen-plus)"
    assert qwen_client.temperature == 0.2

    # 验证 Kimi K2 系列 (如 kimi-k2.6) 自动适配固定 temperature=1.0
    kimi_k2_client = LLMClient(provider="kimi", api_key="sk-test-kimi", model="kimi-k2.6", temperature=0.0)
    assert kimi_k2_client.temperature == 1.0

    # 4. 验证推理模型（如 QwQ / DeepSeek-R1 / 本地 R1）带 <think>...</think> 标签的响应解析
    think_output = """<think>
首先分析第 3.1 条款中的拉伸强度要求...
</think>
{
  "items": [
    {
      "characteristic_name": "抗拉强度 Rm",
      "characteristic_category": "材料",
      "specification": "≥ 500 MPa",
      "inspection_method": "电子万能试验机",
      "acceptance_criteria": "Rm ≥ 500 MPa",
      "standard_clause": "3.1",
      "source_quote": "抗拉强度 Rm 不小于 500 MPa",
      "confidence": "高",
      "needs_human_review": false
    }
  ]
}"""
    parsed_items = LLMClient._parse_items_from_text(think_output)
    assert len(parsed_items) == 1
    assert parsed_items[0]["characteristic_name"] == "抗拉强度 Rm"

    # 5. 验证持久化写入 .env 文件逻辑
    temp_env = tmp_path / ".env"
    temp_env.write_text("DEEPSEEK_API_KEY=sk-old\n", encoding="utf-8")
    persist_llm_config_to_env(
        env_path=temp_env,
        provider="qwen",
        api_key="sk-qwen-12345",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-max",
    )
    env_content = temp_env.read_text(encoding="utf-8")
    assert "LLM_PROVIDER=qwen" in env_content
    assert "DASHSCOPE_API_KEY=sk-qwen-12345" in env_content
    assert "LLM_MODEL=qwen-max" in env_content


def test_feedback_rule_learning_auto_promotion_and_auto_apply():
    """
    测试人工复核自学习闭环：
    当工程师累计 N 次（默认 3 次）将同一待确认项手工准入控制计划后，
    系统自动生成生效规则，后续提取时自动放行并回填工程师指定的检查方法与判据，且同步注入提示词。
    """
    from src.cp_extractor.core.prompt_builder import PromptBuilder

    headers = _get_auth_headers()
    me_resp = client.get("/api/auth/me", headers=headers)
    user_id = me_resp.json()["id"]

    client.post("/api/learning/reset", headers=headers)
    client.put("/api/learning/settings", json={"threshold": 3}, headers=headers)

    try:
        sample_data = client.get("/api/samples/sample_tolerance_iso2768.md").json()

        # 初始状态：提取公差标准样本有 10 条正式项，1 条待人工确认项（局部非关键翻边轮廓度）
        ext1 = client.post(
            "/api/extract",
            json={
                "file_name": "sample_tolerance_iso2768.md",
                "content": sample_data["content"],
                "engine_mode": "rule",
            },
            headers=headers,
        ).json()["package"]
        assert len(ext1["check_items"]) == 10
        assert len(ext1["pending_review_items"]) == 1

        pending_item = ext1["pending_review_items"][0]
        assert pending_item["characteristic_name"] == "局部非关键翻边轮廓度"

        revised_item = {
            **pending_item,
            "specification": "轮廓度 ⌓ ≤ 0.5 mm",
            "inspection_method": "光学三维扫描仪",
            "acceptance_criteria": "实测轮廓度误差 ≤ 0.5 mm",
        }

        # 第 1 次手工准入：计数 1/3，尚未激活
        r1 = client.post(
            "/api/approve-review",
            json={"package": ext1, "review_index": 0, "action": "approve", "updated_item": revised_item},
            headers=headers,
        ).json()["learning_event"]
        assert r1["current_count"] == 1
        assert r1["rule"]["is_active"] is False

        # 第 2 次手工准入：计数 2/3，尚未激活
        r2 = client.post(
            "/api/approve-review",
            json={"package": ext1, "review_index": 0, "action": "approve", "updated_item": revised_item},
            headers=headers,
        ).json()["learning_event"]
        assert r2["current_count"] == 2
        assert r2["rule"]["is_active"] is False

        # 第 3 次手工准入：达到阈值 3/3，自动升格为生效规则！
        r3 = client.post(
            "/api/approve-review",
            json={"package": ext1, "review_index": 0, "action": "approve", "updated_item": revised_item},
            headers=headers,
        ).json()["learning_event"]
        assert r3["current_count"] == 3
        assert r3["just_promoted"] is True
        assert r3["rule"]["is_active"] is True

        # 验证 1：再次运行提取时，门禁自动放行该项并自动回填工程师习惯的检查方法与判据（待确认项变为 0，正式项变为 11）
        ext_after = client.post(
            "/api/extract",
            json={
                "file_name": "sample_tolerance_iso2768.md",
                "content": sample_data["content"],
                "engine_mode": "rule",
            },
            headers=headers,
        ).json()["package"]
        assert len(ext_after["pending_review_items"]) == 0
        assert len(ext_after["check_items"]) == 11
        auto_approved = next(
            i for i in ext_after["check_items"] if i["characteristic_name"] == "局部非关键翻边轮廓度"
        )
        assert auto_approved["inspection_method"] == "光学三维扫描仪"
        assert auto_approved["acceptance_criteria"] == "实测轮廓度误差 ≤ 0.5 mm"
        assert auto_approved["needs_human_review"] is False
        assert "自学习规则已自动准入" in (auto_approved.get("notes") or "")

        # 验证 2：PromptBuilder 组装的提示词中已自动注入该条自学习规则
        assembled_prompt = PromptBuilder(user_id=user_id).build_extraction_prompt("tolerance")
        assert "系统自学习历史人工复核规则" in assembled_prompt
        assert "局部非关键翻边轮廓度" in assembled_prompt
        assert "光学三维扫描仪" in assembled_prompt
    finally:
        client.post("/api/learning/reset", headers=headers)
        client.post("/api/lexicon/reset", json={"category": "all"})


