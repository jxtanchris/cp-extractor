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

    # 1.1 验证防假表格守门与中文稀疏空格自动融合（GB/T 3103.3 前言抗干扰）
    sparse_chinese_sample = (
        "G  B  / T  3 1 0 3 . 3 -  2 0 0 0\n"
        "前  言\n"
        "本 标 准 等 同 采 用 国 际 标 准 IS O 4 7 5 9 - 3 :2 0 0 0 (( 紧 固 件 公 差 第 3 部 分 ：螺 栓 、 螺 钉 和 螺 母 用 平 垫 圈\n"
        "a ） 对 c 级 产 品 未 规 定 h 的 尺 寸 公 差 （ 3 . 1 条 ） ；\n"
    )
    cleaned_doc = DocumentParser._format_space_aligned_tables(sparse_chinese_sample)
    assert "GB/T 3103.3-2000" in cleaned_doc
    assert "前言" in cleaned_doc
    assert "本标准等同采用国际标准 ISO 4759-3:2000 (紧固件公差第3部分：螺栓、螺钉和螺母用平垫圈" in cleaned_doc
    assert "对c级产品未规定h的尺寸公差（3.1条）；" in cleaned_doc
    # 确认普通中文段落绝对没有被误包为 | 列 | 列 | 表格
    assert "| 本 | 标 | 准 |" not in cleaned_doc
    assert "| --- |" not in cleaned_doc

    # 1.2 验证引用标准列表（GB/T 1182、1800.2 等）的防误包表格与跨行延续括号 (eqv ISO) 向上缝合
    raw_reference_standards = (
        "2引用标准\n\n"
        "下列标准所包含的条文，通过在本标准中引用而构成为本标准的条文。\n"
        "| GB/T | 1182-1996形状和位置公差通则、定义、符号和图样表示法（e q v | ISO | 1101:1983) |\n"
        "| --- | --- | --- | --- |\n"
        "| GB/T | 1800.2-1998极限与配合基础第2部分：公差、偏差和配合的基本规定 |  |  |\n"
        "| (eqvISO286-1 | ：1988 ) |  |  |\n"
        "| GB/T | 1800.4-1999极限与配合标准公差等级和孔、轴的极限偏差表（e q v | ISO | 286-2:1996) |\n"
        "| GB/T | 5267-1985螺纹紧固件电镀层 |  |  |\n"
        "| GB/T | 16671-1996形状和位置公差最大实体要求、最小实体要求和可逆要求 |  |  |\n"
        "( eqvISO2692 ： 1996 )\n"
    )
    cleaned_refs = DocumentParser._format_space_aligned_tables(raw_reference_standards)
    assert "## 2 引用标准" in cleaned_refs
    assert "- GB/T 1182-1996 形状和位置公差通则、定义、符号和图样表示法 (eqv ISO 1101:1983)" in cleaned_refs
    assert "- GB/T 1800.2-1998 极限与配合基础第2部分：公差、偏差和配合的基本规定 (eqv ISO 286-1:1988)" in cleaned_refs
    assert "- GB/T 1800.4-1999 极限与配合标准公差等级和孔、轴的极限偏差表 (eqv ISO 286-2:1996)" in cleaned_refs
    assert "- GB/T 5267-1985 螺纹紧固件电镀层" in cleaned_refs
    assert "- GB/T 16671-1996 形状和位置公差最大实体要求、最小实体要求和可逆要求 (eqv ISO 2692:1996)" in cleaned_refs
    # 确保引用标准绝不包含 Markdown 表格符号
    assert "| GB/T |" not in cleaned_refs
    assert "| (eqvISO" not in cleaned_refs

    # 1.3 验证 GB/T 3103.3 第 7 页表 A3 孔的极限偏差（上下偏差纵向折行叠放矩阵融合）
    stacked_tolerance_page7 = (
        "<!-- 第 7 页 -->\n"
        "G  s  /T  3 1 0 3 . 3 -  2 0 0 0\n\n"
        "表A  3孔的极限偏差m  m\n\n"
        "│公称直径  │  极限偏差  │\n\n"
        "│＞      │    │  H    1 3   │  H    1  4   │  H    1  5   │\n\n"
        "│    │3   │  ＋    0  .  1  4 │  十    0  .  2  5 │  十    0  .  4 │\n"
        "│    │    │  0   │  0   │  0   │\n\n"
        "│3    │6   │  ＋    0  .  1  8 │  ＋    0  .  3 │  十    0  .  4  8 │\n"
        "│    │    │  0   │  0   │  0   │\n\n"
        "│6   │1  0  │  十    0  .  2  2 │  十    0  .  3  6 │  十     0  .  5 8  │\n"
        "│    │    │  0   │  0   │  0   │\n"
    )
    cleaned_p7 = DocumentParser._format_space_aligned_tables(stacked_tolerance_page7)
    assert "### 表 A3 孔的极限偏差(mm)" in cleaned_p7
    assert "| 基本尺寸分段 (mm) | H13 | H14 | H15 |" in cleaned_p7
    assert "| ≤ 3 | +0.14 / 0 | +0.25 / 0 | +0.4 / 0 |" in cleaned_p7
    assert "| >3 ~ 6 | +0.18 / 0 | +0.3 / 0 | +0.48 / 0 |" in cleaned_p7
    assert "| >6 ~ 10 | +0.22 / 0 | +0.36 / 0 | +0.58 / 0 |" in cleaned_p7

    # 1.4 验证第 6 页及任意新文件的通用抽象化（表 A1 平垫圈厚度公差：A级/C级上下偏差通用折叠）
    stacked_tolerance_page6 = (
        "表A  1平垫圈厚度公差m  m\n\n"
        "│公称厚度  │  极限偏差  │\n\n"
        "│＞      │    │  A 级  │  C 级  │\n\n"
        "│    │1.5 │  +0.05 │ +0.10 │\n"
        "│    │    │  -0.05 │ -0.10 │\n\n"
        "│1.5  │3   │  +0.08 │ +0.15 │\n"
        "│    │    │  -0.08 │ -0.15 │\n"
    )
    cleaned_p6 = DocumentParser._format_space_aligned_tables(stacked_tolerance_page6)
    assert "### 表 A1 平垫圈厚度公差(mm)" in cleaned_p6
    assert "| 基本尺寸分段 (mm) | A级 | C级 |" in cleaned_p6
    assert "| ≤ 1.5 | +0.05 / -0.05 | +0.10 / -0.10 |" in cleaned_p6
    assert "| >1.5 ~ 3 | +0.08 / -0.08 | +0.15 / -0.15 |" in cleaned_p6

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
    assert cell_p1.hyperlink.target.startswith("http://127.0.0.1:8000")
    assert cell_p1.hyperlink.target.endswith("/api/source/steering_knuckle_source.pdf#page=1")
    assert "3.2 (P.2)" in str(cell_p2.value)
    assert cell_p2.hyperlink.target.endswith("/api/source/steering_knuckle_source.pdf#page=2")
    assert "5.3 (P.3)" in str(cell_p3.value)
    assert cell_p3.hyperlink.target.endswith("/api/source/steering_knuckle_source.pdf#page=3")


def test_v2_grill_me_architectural_decisions(tmp_path: Path):
    """
    验证 v2.0 规格说明书（Grill-Me 五大核心分支决策）：
    1. 块级动态路由与单块多插件融合 (classify_chunk_plugins)
    2. 自学习硬门禁绝对优先（不绕过幻觉/篡改）与严格核心名称匹配 + 强制条款防误删保护
    3. 二次自纠错精准范围隔离（不送修单纯低置信度项）与零丢失对账 (_reconcile_refined_items)
    4. CheckItem v2.0 15字段 IATF 16949 扩展与自学习记忆回填
    """
    from src.cp_extractor.core.classifier import StandardClassifier
    from src.cp_extractor.core.extractor import StandardExtractorPipeline
    from src.cp_extractor.core.feedback_learner import FeedbackRuleLearner
    from src.cp_extractor.core.lexicon_db import LexiconDB

    # 分支 1：测试复合型块级多插件路由
    classifier = StandardClassifier()
    composite_chunk = (
        "3.1 钢的化学成分熔炼分析、屈服强度与抗拉强度必须符合规定。\n"
        "4.1 未注公差与形位公差 GD&T 位置度必须符合 ISO 2768。"
    )
    plugins = classifier.classify_chunk_plugins(composite_chunk, default_plugin_key="tolerance")
    assert "material" in plugins and "tolerance" in plugins

    # 分支 2 & 4：测试自学习硬门禁绝对优先、严格核心名称匹配、强制条款防误删及 v2.0 扩展字段回填
    db_file = tmp_path / "test_lex.db"
    lex_db = LexiconDB(db_path=db_file)
    learner = FeedbackRuleLearner(db_path=db_file)
    learner.set_threshold(1)
    validator = QualityGateValidator(lexicon_db=lex_db, feedback_learner=learner)

    # 录入一条带 v2.0 扩展字段的 approve 自学习规则
    approved_sample = CheckItem(
        characteristic_name="局部非关键翻边轮廓度",
        characteristic_category=CharacteristicCategory.DIMENSION,
        special_char_class="SC",
        specification="轮廓度 ⌓ ≤ 0.5 mm",
        inspection_method="三坐标测量机",
        acceptance_criteria="轮廓度 ≤ 0.5 mm",
        sample_size_frequency="5件/每班",
        control_method="SPC Xbar-R图",
        reaction_plan="立即停机隔离并100%全检追溯",
        standard_clause="5.4",
        source_quote="局部非关键翻边轮廓度建议参考历史经验控制",
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )
    learner.record_review_decision("公差/尺寸标准", approved_sample, action="approve", updated_item=approved_sample)

    # 当该项出现硬性幻觉错误（原文引用失配）时，自学习 approve 严禁绕过硬门禁，但应回填 v2.0 扩展字段
    hallucinated_item = CheckItem(
        characteristic_name="局部非关键翻边轮廓度",
        characteristic_category=CharacteristicCategory.DIMENSION,
        specification="待定",
        inspection_method="待确认",
        acceptance_criteria="待确认",
        standard_clause="5.4",
        source_quote="一段原文中根本不存在的幻觉引文",
        confidence=ConfidenceLevel.LOW,
        needs_human_review=True,
    )
    pkg_hard = ExtractionPackage(
        standard_info=StandardInfo(file_name="test.pdf", standard_category="公差/尺寸标准"),
        check_items=[hallucinated_item],
    )
    res_hard = validator.validate_package(pkg_hard, source_full_text="5.4 局部非关键翻边轮廓度建议参考历史经验控制。")
    assert len(res_hard.check_items) == 0
    assert len(res_hard.pending_review_items) == 1
    assert res_hard.pending_review_items[0].special_char_class == "SC"
    assert res_hard.pending_review_items[0].sample_size_frequency == "5件/每班"
    assert res_hard.pending_review_items[0].control_method == "SPC Xbar-R图"
    assert res_hard.pending_review_items[0].reaction_plan == "立即停机隔离并100%全检追溯"

    # 测试严格核心名称匹配（驳回「翻边轮廓度」不得模糊误伤「关键安装面翻边轮廓度」）与高置信度强制条款防静默删除
    rej_sample = CheckItem(
        characteristic_name="翻边轮廓度",
        characteristic_category=CharacteristicCategory.DIMENSION,
        specification="参考",
        inspection_method="待定",
        acceptance_criteria="待定",
        standard_clause="5.5",
        source_quote="翻边轮廓度",
        confidence=ConfidenceLevel.LOW,
        needs_human_review=True,
    )
    learner.record_review_decision("公差/尺寸标准", rej_sample, action="reject")
    assert learner.match_active_rule("公差/尺寸标准", "关键安装面翻边轮廓度") is None
    assert learner.match_active_rule("公差/尺寸标准", "【冲压工序】翻边轮廓度") is not None

    # 当新文档中「翻边轮廓度」是高置信度明确公差强制项时，命中 reject 规则应转入待确认清单而非静默删除
    mandatory_item = CheckItem(
        characteristic_name="翻边轮廓度",
        characteristic_category=CharacteristicCategory.DIMENSION,
        specification="⌓ 0.20 mm A B",
        inspection_method="三坐标",
        acceptance_criteria="轮廓度 ≤ 0.20 mm",
        standard_clause="5.5",
        source_quote="5.5 翻边轮廓度 ⌓ 0.20 mm A B",
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )
    pkg_rej = ExtractionPackage(
        standard_info=StandardInfo(file_name="test2.pdf", standard_category="公差/尺寸标准"),
        check_items=[mandatory_item],
    )
    res_rej = validator.validate_package(pkg_rej, source_full_text="5.5 翻边轮廓度 ⌓ 0.20 mm A B")
    assert len(res_rej.pending_review_items) == 1
    assert "防误删" in (res_rej.pending_review_items[0].notes or "")

    # 分支 3：测试二次自纠错仅送修硬错误项（保留单纯低置信度项）且在 LLM 漏返时零丢失兜底
    sent_to_refiner = []

    def mock_extract(_p: str) -> str:
        import json
        return json.dumps([
            {
                "standard_clause": "3.1",
                "source_quote": "孔径 Ø16.00 +0.05/-0.00 mm",
                "characteristic_name": "硬错误单边公差项",
                "characteristic_category": "尺寸",
                "specification": "Ø16.00 ± 0.05 mm",
                "inspection_method": "塞规",
                "acceptance_criteria": "Ø16.00 ± 0.05 mm",
                "confidence": "高",
                "needs_human_review": False,
            },
            {
                "standard_clause": "3.2",
                "source_quote": "倒角建议参考经验控制",
                "characteristic_name": "单纯低置信度建议项",
                "characteristic_category": "尺寸",
                "specification": "参考经验",
                "inspection_method": "待定",
                "acceptance_criteria": "待定",
                "confidence": "低",
                "needs_human_review": True,
            },
        ], ensure_ascii=False)

    def mock_empty_refiner(src: str, failed_items: list, errors: list, val_tpl: str) -> list:
        sent_to_refiner.extend(i["characteristic_name"] for i in failed_items)
        # 模拟 LLM 在重提时异常漏返空列表
        return []

    pipe = StandardExtractorPipeline(llm_callable=mock_extract, llm_refiner=mock_empty_refiner)
    res_pipe = pipe.run(
        "test_zero_drop.md",
        "3.1 孔径 Ø16.00 +0.05/-0.00 mm\n3.2 倒角建议参考经验控制",
        manual_category="tolerance",
    )
    assert sent_to_refiner == ["硬错误单边公差项"]
    assert len(res_pipe.pending_review_items) == 2
