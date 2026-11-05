import pytest
from pathlib import Path
from src.cp_extractor.schemas.models import (
    CheckItem,
    CharacteristicCategory,
    ConfidenceLevel,
    StandardInfo,
    ExtractionPackage,
)
from src.cp_extractor.core.prompt_builder import PromptBuilder
from src.cp_extractor.core.validator import QualityGateValidator
from src.cp_extractor.core.mapper import ControlPlanExcelMapper


def test_models_enforce_low_confidence():
    """测试低置信度自动标记人工审核门禁"""
    item = CheckItem(
        characteristic_name="抗拉强度 Rm",
        characteristic_category=CharacteristicCategory.MATERIAL,
        specification="≥ 400 MPa",
        inspection_method="拉伸试验",
        acceptance_criteria="抗拉强度 ≥ 400 MPa",
        standard_clause="5.2",
        source_quote="抗拉强度 Rm 不应小于 400 MPa",
        confidence=ConfidenceLevel.LOW,
        needs_human_review=False,  # 传入 False 应被 validator 自动修正为 True
    )
    assert item.needs_human_review is True


def test_prompt_builder():
    """测试提示词构建器读取与装配"""
    builder = PromptBuilder()
    
    # 基础模板
    base = builder.get_base_template()
    assert "质量工程师" in base
    assert "CTQ" in base
    
    # 4类插件装配
    categories = ["tolerance", "material", "process", "appearance_performance"]
    for cat in categories:
        prompt = builder.build_extraction_prompt(cat, context_text="示例标准文本")
        assert "示例标准文本" in prompt
        assert "输出要求" in prompt


def test_ctq_reference_dim_and_unilateral_tolerance_gate():
    """测试融合自 CTQ-extraction 的括号参考尺寸拦截与单边公差防篡改门禁"""
    validator = QualityGateValidator()

    # 1. 括号参考尺寸 (120) 应被拦截至 pending_review_items
    ref_dim_item = CheckItem(
        characteristic_name="辅助总长",
        characteristic_category=CharacteristicCategory.DIMENSION,
        specification="(120)",
        inspection_method="卡尺",
        acceptance_criteria="参考尺寸 (120) mm",
        standard_clause="3.1",
        source_quote="辅助总长 (120)",
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )

    # 2. 原文为单边公差 +0.05/-0.00，但被错误改写为 ±0.05 应被拦截
    tampered_tol_item = CheckItem(
        characteristic_name="内孔直径",
        characteristic_category=CharacteristicCategory.DIMENSION,
        specification="Ø25 ± 0.05 mm",
        inspection_method="三坐标",
        acceptance_criteria="Ø25 ± 0.05 mm",
        standard_clause="3.2",
        source_quote="内孔直径 Ø25 +0.05/-0.00 mm",
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )

    pkg = ExtractionPackage(
        standard_info=StandardInfo(file_name="DRAWING_CTQ.pdf", standard_category="公差/尺寸标准"),
        check_items=[ref_dim_item, tampered_tol_item],
    )
    res = validator.validate_package(pkg)
    assert len(res.check_items) == 0
    assert len(res.pending_review_items) == 2
    assert any("括号参考尺寸" in s for s in res.validation_report.suggestions)
    assert any("单边公差" in m for m in res.validation_report.missing_clause_or_quote)


def test_quality_gate_validator():
    """测试质量门禁与反幻觉核对"""
    source_text = "第6.1条：冷轧板表面粗糙度Ra应在0.8μm至1.6μm之间，使用粗糙度仪测定。"
    
    # 项 1：正常合规项
    item1 = CheckItem(
        characteristic_name="表面粗糙度 Ra",
        characteristic_category=CharacteristicCategory.SURFACE if hasattr(CharacteristicCategory, "SURFACE") else CharacteristicCategory.APPEARANCE,
        specification="0.8 - 1.6 μm",
        inspection_method="粗糙度仪",
        acceptance_criteria="0.8 μm ≤ Ra ≤ 1.6 μm",
        standard_clause="6.1",
        source_quote="表面粗糙度Ra应在0.8μm至1.6μm之间",
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )

    # 项 2：试验方法误写到判定准则，且原文引用不存在（幻觉测试）
    item2 = CheckItem(
        characteristic_name="屈服强度",
        characteristic_category=CharacteristicCategory.MATERIAL,
        specification="250 MPa",
        inspection_method="未知",
        acceptance_criteria="使用电子万能试验机拉伸检验",  # 误写试验方法
        standard_clause="",  # 缺少条款
        source_quote="某不存在的假冒引用语句",
        confidence=ConfidenceLevel.LOW,
        needs_human_review=False,
    )

    package = ExtractionPackage(
        standard_info=StandardInfo(file_name="GB_T_TEST.pdf", standard_category="材料标准"),
        check_items=[item1, item2],
    )

    validator = QualityGateValidator()
    result = validator.validate_package(package, source_full_text=source_text)

    # item1 应留在 check_items
    assert len(result.check_items) == 1
    assert result.check_items[0].characteristic_name == "表面粗糙度 Ra"

    # item2 应被推入 pending_review_items (门禁拦截)
    assert len(result.pending_review_items) == 1
    assert result.pending_review_items[0].characteristic_name == "屈服强度"
    assert len(result.validation_report.missing_clause_or_quote) >= 1
    assert len(result.validation_report.method_misclassified_as_criteria) >= 1


def test_excel_export(tmp_path: Path):
    """测试 Excel 导出功能"""
    item = CheckItem(
        characteristic_name="总长尺寸",
        characteristic_category=CharacteristicCategory.DIMENSION,
        specification="120 ± 0.2 mm",
        inspection_method="数显卡尺",
        acceptance_criteria="119.8 - 120.2 mm",
        standard_clause="4.1",
        source_quote="零件总长应为 120±0.2mm",
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )
    package = ExtractionPackage(
        standard_info=StandardInfo(file_name="ISO_13920.pdf", standard_category="公差/尺寸标准"),
        check_items=[item],
    )
    package.refresh_summary()

    mapper = ControlPlanExcelMapper()
    export_file = tmp_path / "output_cp.xlsx"
    res_path = mapper.export(package, export_file)
    assert res_path.exists()


def test_pdf_table_alignment_and_document_chunking():
    """测试维度3：PDF 多空格对齐表格转 Markdown 表格，以及长文档按章节分块与去重"""
    from src.cp_extractor.core.parser import DocumentParser
    from src.cp_extractor.core.extractor import StandardExtractorPipeline

    # 1. 验证多空格对齐表格自动转换为 Markdown 表格
    raw_pdf_layout_text = (
        "4.1 未注线性尺寸公差表\n"
        "基本尺寸分段(mm)    f(精密级)    m(中等级)    c(粗糙级)\n"
        ">0.5 ~ 3           ±0.05        ±0.10        ±0.20\n"
        ">3 ~ 6             ±0.05        ±0.10        ±0.30\n"
    )
    formatted = DocumentParser._format_space_aligned_tables(raw_pdf_layout_text)
    assert "| 基本尺寸分段(mm) | f(精密级) | m(中等级) | c(粗糙级) |" in formatted
    assert "| --- | --- | --- | --- |" in formatted
    assert "| >0.5 ~ 3 | ±0.05 | ±0.10 | ±0.20 |" in formatted

    # 2. 验证长文档按章节语义分块并保留顶部标题
    long_doc = (
        "# Q/CP 001-2026 汽车齿轮技术标准\n\n"
        "## 3. 尺寸与形位公差\n" + ("3.1 孔径 Ø16.00 +0.05/-0.00 mm。\n" * 60) + "\n\n"
        "## 4. 热处理工艺要求\n" + ("4.1 渗碳温度 920 ± 10 ℃。\n" * 60)
    )
    chunks = StandardExtractorPipeline.split_document_into_chunks(long_doc, max_chars=1200)
    assert len(chunks) >= 2
    for ch in chunks:
        assert "Q/CP 001-2026" in ch


def test_self_correction_refinement_loop():
    """测试维度2：当 LLM 首次输出存在单边公差改写或引用偏差时，自动触发二次校验修正闭环"""
    import json
    from src.cp_extractor.core.extractor import StandardExtractorPipeline

    source_text = "## 3. 尺寸要求\n3.1 主定位孔孔径 D1 必须控制在 Ø16.00 +0.05/-0.00 mm，采用塞规检验。"

    # 模拟首次 LLM 提取犯错：将单边公差 +0.05/-0.00 错误改写为 ±0.05
    def mock_first_extract(_prompt: str) -> str:
        return json.dumps([
            {
                "standard_clause": "3.1",
                "source_quote": "主定位孔孔径 D1 必须控制在 Ø16.00 +0.05/-0.00 mm，采用塞规检验。",
                "characteristic_name": "主定位孔孔径 D1",
                "characteristic_category": "尺寸",
                "specification": "Ø16.00 ± 0.05 mm",  # 故意篡改单边公差以触发门禁
                "inspection_method": "塞规",
                "acceptance_criteria": "Ø16.00 ± 0.05 mm",
                "confidence": "高",
                "needs_human_review": False,
            }
        ], ensure_ascii=False)

    # 模拟二次自纠错器收到门禁错误报告后修复为正确的单边公差
    refine_called = {"count": 0}

    def mock_refiner(src: str, failed_items: list, errors: list, val_tpl: str) -> list:
        refine_called["count"] += 1
        assert any("单边公差" in e for e in errors)
        fixed = dict(failed_items[0])
        fixed["specification"] = "Ø16.00 +0.05/-0.00 mm"
        fixed["acceptance_criteria"] = "Ø16.00 ~ Ø16.05 mm"
        fixed["needs_human_review"] = False
        fixed["notes"] = None
        return [fixed]

    pipeline = StandardExtractorPipeline(
        llm_callable=mock_first_extract,
        llm_refiner=mock_refiner,
    )
    pkg = pipeline.run("test_self_correct.md", source_text, manual_category="tolerance")

    assert refine_called["count"] == 1
    assert len(pkg.check_items) == 1
    assert len(pkg.pending_review_items) == 0
    assert pkg.check_items[0].specification == "Ø16.00 +0.05/-0.00 mm"


def test_pdf_clause_page_resolution_and_excel_hyperlink(tmp_path: Path):
    """测试条款自动定位 PDF 页码（source_page）并在导出的 Excel 中生成 #page=N 超链接"""
    import openpyxl
    from src.cp_extractor.core.extractor import StandardExtractorPipeline

    multi_page_pdf_text = (
        "<!-- 第 1 页 -->\n"
        "# Q/CP 101-2026 转向节尺寸标准\n"
        "3.1 主定位孔孔径 D1 必须控制在 Ø16.00 +0.05/-0.00 mm，采用气动量仪或塞规进行 100% 检验。\n\n"
        "<!-- 第 2 页 -->\n"
        "3.2 前后安装孔中心距 L1 应为 240.0 ± 0.15 mm，使用三坐标测量机（CMM）按首末件及每班抽检。\n\n"
        "<!-- 第 3 页 -->\n"
        "5.3 主销孔相对于基准 A、B、C 的位置度（⌖）在最大实体要求（Ⓜ）下不得大于 Ø0.12 mm，使用专用检具或三坐标测量机检验。"
    )

    pipeline = StandardExtractorPipeline()
    pkg = pipeline.run("steering_knuckle.pdf", multi_page_pdf_text, manual_category="tolerance")
    pkg.standard_info.source_file_url = "/api/source/steering_knuckle_source.pdf"

    page_by_clause = {item.standard_clause: item.source_page for item in pkg.check_items}
    assert page_by_clause.get("3.1") == 1
    assert page_by_clause.get("3.2") == 2
    assert page_by_clause.get("5.3") == 3

    mapper = ControlPlanExcelMapper()
    excel_file = tmp_path / "linked_cp.xlsx"
    mapper.export(pkg, excel_file)

    wb = openpyxl.load_workbook(excel_file)
    ws = wb["控制计划检查项"]
    # 第 3 行对应条款 3.1 (P.1)，第 4 行对应条款 3.2 (P.2)，第 5 行对应条款 5.3 (P.3)
    cell_p1 = ws.cell(row=3, column=7)
    cell_p2 = ws.cell(row=4, column=7)
    cell_p3 = ws.cell(row=5, column=7)
    assert "3.1 (P.1)" in str(cell_p1.value)
    assert cell_p1.hyperlink.target.endswith("/api/source/steering_knuckle_source.pdf#page=1")
    assert "3.2 (P.2)" in str(cell_p2.value)
    assert cell_p2.hyperlink.target.endswith("/api/source/steering_knuckle_source.pdf#page=2")
    assert "5.3 (P.3)" in str(cell_p3.value)
    assert cell_p3.hyperlink.target.endswith("/api/source/steering_knuckle_source.pdf#page=3")
