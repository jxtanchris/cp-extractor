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


def test_source_quote_normalization_and_fuzzy_tolerance():
    """测试 Phase 2：反幻觉门禁标点归一化与高阈值模糊容差机制"""
    source_text = (
        "# 技术要求\n\n"
        "3.1 销轴外径：尺寸应为 Ø25.00 (+0.02/-0.01) mm，采用千分尺检测。\n"
        "3.2 热处理淬火硬度：必须达到 45~50 HRC，按 GB/T 230.1 洛氏硬度计测试。\n"
    )

    validator = QualityGateValidator()

    # 1. 验证仅存在标点差异（如冒号全半角、括号差异、省略表格符号等）但内容一致时：
    # 标点归一化后匹配度 >= 90%，判定为合格放行，绝不产生硬错误误报
    item_punc_diff = CheckItem(
        characteristic_name="销轴外径",
        characteristic_category=CharacteristicCategory.DIMENSION,
        specification="Ø25.00 +0.02/-0.01 mm",
        inspection_method="千分尺",
        acceptance_criteria="Ø24.99 ~ Ø25.02 mm",
        standard_clause="3.1",
        # 原文为 "3.1 销轴外径：尺寸应为 Ø25.00 (+0.02/-0.01) mm，采用千分尺检测。"
        # 模型输出带有全半角/空格/管道符细微格式差: "销轴外径: 尺寸应为 Ø25.00 (+0.02/-0.01) mm, 采用千分尺检测"
        source_quote="销轴外径: 尺寸应为 Ø25.00 (+0.02/-0.01) mm, 采用千分尺检测",
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )
    pkg1 = ExtractionPackage(
        standard_info=StandardInfo(file_name="test.pdf", standard_category="公差/尺寸标准"),
        check_items=[item_punc_diff],
    )
    res1 = validator.validate_package(pkg1, source_full_text=source_text)
    # 应直接通过，不触发 missing_clause_or_quote 硬错误，留在 check_items
    assert len(res1.validation_report.missing_clause_or_quote) == 0
    assert len(res1.check_items) == 1
    assert res1.check_items[0].characteristic_name == "销轴外径"

    # 2. 验证轻微概括改写（吻合度 75% ~ 90%）：
    # 分流至待人工复核清单，但绝不记为硬错误（避免触发昂贵的模型二次自纠错重提）
    item_mild_rewrite = CheckItem(
        characteristic_name="热处理淬火硬度",
        characteristic_category=CharacteristicCategory.PROCESS_PARAMETER,
        specification="45~50 HRC",
        inspection_method="洛氏硬度计",
        acceptance_criteria="45~50 HRC",
        standard_clause="3.2",
        # 模型省略了部分定语: "淬火硬度达到 45~50 HRC 洛氏硬度计测试"
        source_quote="淬火硬度达到 45~50 HRC 洛氏硬度计测试",
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )
    pkg2 = ExtractionPackage(
        standard_info=StandardInfo(file_name="test.pdf", standard_category="工艺标准"),
        check_items=[item_mild_rewrite],
    )
    res2 = validator.validate_package(pkg2, source_full_text=source_text)
    # 不属于硬错误，不应出现在 missing_clause_or_quote 中
    assert len(res2.validation_report.missing_clause_or_quote) == 0
    # 但属于待人工确认软门禁
    assert len(res2.pending_review_items) == 1
    assert res2.pending_review_items[0].characteristic_name == "热处理淬火硬度"
    assert any("吻合度" in s for s in res2.validation_report.suggestions)

    # 3. 验证真正编造的幻觉引用（吻合度 < 75%）：
    # 必须坚决拦截记入 missing_clause_or_quote 硬错误
    item_fake = CheckItem(
        characteristic_name="抗拉强度",
        characteristic_category=CharacteristicCategory.MATERIAL,
        specification="≥ 600 MPa",
        inspection_method="拉伸机",
        acceptance_criteria="≥ 600 MPa",
        standard_clause="3.3",
        source_quote="完全凭空捏造的虚假引用文本没有任何原文依据",
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )
    pkg3 = ExtractionPackage(
        standard_info=StandardInfo(file_name="test.pdf", standard_category="材料标准"),
        check_items=[item_fake],
    )
    res3 = validator.validate_package(pkg3, source_full_text=source_text)
    assert len(res3.validation_report.missing_clause_or_quote) == 1
    assert "可能存在改写或幻觉" in res3.validation_report.missing_clause_or_quote[0]


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


def test_sticky_table_headers_and_caption_inheritance():
    """测试 Phase 1：大表格切分或二分时表头/表题动态吸顶继承 (Sticky Table Header & Caption)"""
    from src.cp_extractor.core.extractor import StandardExtractorPipeline

    # 1. 验证 _split_text_in_half 对截断表格自动注入表头与表题 (续)
    table_text = (
        "### 表 1 线性尺寸极限偏差数值 (mm)\n"
        "| 基本尺寸分段 | 精密 f | 中等 m | 粗糙 c |\n"
        "|---|---|---|---|\n"
        "| 0.5~3 | ±0.05 | ±0.1 | ±0.2 |\n"
        "| >3~6 | ±0.05 | ±0.1 | ±0.3 |\n"
        "| >6~30 | ±0.1 | ±0.2 | ±0.5 |\n"
        "| >30~120 | ±0.15 | ±0.3 | ±0.8 |\n"
        "| >120~400 | ±0.2 | ±0.5 | ±1.2 |\n"
    )
    halves = StandardExtractorPipeline._split_text_in_half(table_text)
    assert len(halves) == 2, "应成功切分为两部分"
    half1, half2 = halves[0], halves[1]

    # half1 包含表头与前部数据行
    assert "### 表 1 线性尺寸极限偏差数值 (mm)" in half1
    assert "| 0.5~3 | ±0.05 |" in half1

    # half2 必须自动继承吸顶表题 (续) 和列名表头，杜绝断头表格
    assert "### 表 1 线性尺寸极限偏差数值 (mm) (续)" in half2
    assert "| 基本尺寸分段 | 精密 f | 中等 m | 粗糙 c |" in half2
    assert "|---|---|---|---|" in half2
    assert "| >120~400 | ±0.2 |" in half2

    # 2. 验证多页大表格在 split_document_into_chunks 中切分时自动继承表题与表头
    multi_row_table = (
        "# GB/T 1804-2000 一般公差\n\n"
        "## 5. 公差数值表\n"
        "### 表 2 倒圆半径与倒角高度极限偏差 (mm)\n"
        "| 基本尺寸分段 | 精密 f / 中等 m | 粗糙 c / 最粗 v |\n"
        "|---|---|---|\n"
    )
    for i in range(1, 35):
        multi_row_table += f"| 分段_{i} ({i*10}~{i*10+10}) | ±{i*0.1:.1f} | ±{i*0.2:.1f} |\n"

    chunks = StandardExtractorPipeline.split_document_into_chunks(multi_row_table, max_chars=600)
    assert len(chunks) >= 2, "大表格按 600 字符限制应至少切分为 2 个分块"

    # 每个含有该表后续数据行的分块，都必须拥有表题与完整表头
    for idx, ch in enumerate(chunks):
        if idx == 0:
            assert "### 表 2 倒圆半径与倒角高度极限偏差 (mm)" in ch
            assert "| 基本尺寸分段 | 精密 f / 中等 m |" in ch
        else:
            # 后续分块必须吸顶继承 (续) 与表头
            assert "### 表 2 倒圆半径与倒角高度极限偏差 (mm) (续)" in ch
            assert "| 基本尺寸分段 | 精密 f / 中等 m | 粗糙 c / 最粗 v |" in ch
            assert "|---|---|---|" in ch


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


def test_targeted_refinement_context_for_long_document():
    """测试 Phase 3：长文档二次自纠错时定向局部上下文切片送修 (Targeted Local Context)"""
    import json
    from src.cp_extractor.core.extractor import StandardExtractorPipeline
    from src.cp_extractor.schemas.models import CheckItem, CharacteristicCategory, ConfidenceLevel

    # 构造一份包含 5 页、长达上万字的典型大型标准文档
    page1 = "<!-- 第 1 页 -->\n# Q/CP 888-2026 传动轴总成综合标准\n" + ("1.1 总则说明及适用范围文字。\n" * 80)
    page2 = "<!-- 第 2 页 -->\n## 2. 规范性引用文件\n" + ("2.1 引用国家标准 GB/T 1804 及 ISO 2768。\n" * 80)
    page3 = (
        "<!-- 第 3 页 -->\n## 3. 关键尺寸与公差\n"
        "3.1 花键轴端部外径 D 必须控制在 Ø32.00 +0.03/-0.00 mm，使用外径千分尺检验。\n"
        + ("3.2 普通未注倒角为 1.0 ± 0.2 mm。\n" * 80)
    )
    page4 = "<!-- 第 4 页 -->\n## 4. 表面防腐要求\n" + ("4.1 表面电泳涂层厚度应为 15~25 μm。\n" * 80)
    page5 = "<!-- 第 5 页 -->\n## 5. 包装与入库规范\n" + ("5.1 包装箱需放置防潮防锈剂，堆叠不超过3层。\n" * 80)

    long_multi_page_doc = f"{page1}\n\n{page2}\n\n{page3}\n\n{page4}\n\n{page5}"
    assert len(long_multi_page_doc) > 8000, "构造的长文档应超过 8000 字符"

    # 1. 直接测试 extract_targeted_refinement_context 方法
    err_item = CheckItem(
        characteristic_name="花键轴端部外径 D",
        characteristic_category=CharacteristicCategory.DIMENSION,
        specification="Ø32.00 ± 0.03 mm",  # 发生单边篡改
        inspection_method="千分尺",
        acceptance_criteria="Ø32.00 ~ Ø32.03 mm",
        standard_clause="3.1",
        source_quote="花键轴端部外径 D 必须控制在 Ø32.00 +0.03/-0.00 mm，使用外径千分尺检验。",
        source_page=3,
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )

    targeted_ctx = StandardExtractorPipeline.extract_targeted_refinement_context(
        long_multi_page_doc, [err_item]
    )

    # 验证上下文瘦身与精准定位：
    # 1) 长度远小于全文（通常在 1000~3000 字之间，而不是 >8000 字）
    assert len(targeted_ctx) < len(long_multi_page_doc) * 0.5
    # 2) 必须保留文档首部标题信息
    assert "Q/CP 888-2026" in targeted_ctx
    # 3) 必须精准包含出错项所在第 3 页/条款 3.1 的内容
    assert "3.1 花键轴端部外径 D" in targeted_ctx
    # 4) 不应包含遥远且无关的第 5 页包装文字
    assert "包装箱需放置防潮防锈剂" not in targeted_ctx

    # 2. 验证端到端提取流中 llm_refiner 确实收到了瘦身后的定向上下文
    refiner_received_src = []

    def mock_extract_with_err(_prompt: str) -> str:
        return json.dumps([
            {
                "standard_clause": "3.1",
                "source_page": 3,
                "source_quote": "花键轴端部外径 D 必须控制在 Ø32.00 +0.03/-0.00 mm，使用外径千分尺检验。",
                "characteristic_name": "花键轴端部外径 D",
                "characteristic_category": "尺寸",
                "specification": "Ø32.00 ± 0.03 mm",  # 篡改单边公差触发门禁
                "inspection_method": "千分尺",
                "acceptance_criteria": "Ø32.00 ~ Ø32.03 mm",
                "confidence": "高",
                "needs_human_review": False,
            }
        ], ensure_ascii=False)

    def mock_targeted_refiner(src: str, failed_items: list, errors: list, val_tpl: str) -> list:
        refiner_received_src.append(src)
        fixed = dict(failed_items[0])
        fixed["specification"] = "Ø32.00 +0.03/-0.00 mm"
        fixed["needs_human_review"] = False
        return [fixed]

    pipe = StandardExtractorPipeline(
        llm_callable=mock_extract_with_err,
        llm_refiner=mock_targeted_refiner,
    )
    res_pkg = pipe.run("test_long.md", long_multi_page_doc, manual_category="tolerance")

    assert len(refiner_received_src) == 1
    passed_src = refiner_received_src[0]
    # 送修文本被精准裁剪，杜绝全文档 Token 浪费
    assert len(passed_src) < len(long_multi_page_doc) * 0.5
    assert "3.1 花键轴端部外径 D" in passed_src
    assert res_pkg.check_items[0].specification == "Ø32.00 +0.03/-0.00 mm"


def test_feedback_prompt_specification_decoupling_and_anti_pollution(tmp_path: Path):
    """测试 Phase 4：自学习规则注入提示词时「规范数值」与「工艺量检具」解耦防污染保护"""
    from src.cp_extractor.core.feedback_learner import FeedbackRuleLearner
    from src.cp_extractor.core.extractor import StandardExtractorPipeline
    from src.cp_extractor.schemas.models import CheckItem, CharacteristicCategory, ConfidenceLevel

    test_db = tmp_path / "test_feedback_anti_pollute.db"
    learner = FeedbackRuleLearner(db_path=test_db, user_id=101)

    # 1. 模拟工程师对零件 A 历史学习了具体尺寸（如 10.00 +0.02/-0.00 mm）和通用量检具习惯
    hist_item = CheckItem(
        characteristic_name="定位销外径",
        characteristic_category=CharacteristicCategory.DIMENSION,
        specification="Ø10.00 +0.02/-0.00 mm",
        inspection_method="高精度数显千分尺",
        acceptance_criteria="Ø10.00 ~ Ø10.02 mm",
        sample_size_frequency="5件/每班",
        control_method="SPC控制图",
        reaction_plan="停机隔离并通知班长",
        standard_clause="3.1",
        source_quote="定位销外径尺寸为 Ø10.00 +0.02/-0.00 mm",
        confidence=ConfidenceLevel.HIGH,
        needs_human_review=False,
    )
    # 手工准入达到阈值 3 次，使其升格生效
    for _ in range(3):
        learner.record_review_decision(
            standard_category="tolerance",
            original_item=hist_item,
            action="approve",
            updated_item=hist_item,
        )

    # 2. 验证 build_prompt_injection_block：
    # - 必须包含防污染强约束说明
    # - 优先继承工艺方法（千分尺、频次、控制方法、反应计划）
    # - 绝对严禁把历史具体数值写成“规范填「Ø10.00 +0.02/-0.00 mm」”来误导模型！
    prompt_block = learner.build_prompt_injection_block("tolerance")
    assert "防污染" in prompt_block
    assert "定位销外径" in prompt_block
    assert "高精度数显千分尺" in prompt_block
    assert "SPC控制图" in prompt_block
    assert "规范填「Ø10.00 +0.02/-0.00 mm」" not in prompt_block
    assert "严格以当前原文为准" in prompt_block

    # 3. 验证端到端提取零件 B（新尺寸 Ø28.00 +0.05/-0.00 mm）时：
    # 尺寸绝不被历史 Ø10.00 污染篡改，而通用工艺属性成功享受自学习红利！
    new_doc = "## 3. 尺寸规范\n3.1 定位销外径必须保证 Ø28.00 +0.05/-0.00 mm，请严格按规范受控。"

    def mock_extract_new_part(_prompt: str) -> str:
        import json
        # 验证提示词中带有防污染指令
        assert "防污染" in _prompt
        return json.dumps([
            {
                "standard_clause": "3.1",
                "source_quote": "定位销外径必须保证 Ø28.00 +0.05/-0.00 mm，请严格按规范受控。",
                "characteristic_name": "定位销外径",
                "characteristic_category": "尺寸",
                "specification": "Ø28.00 +0.05/-0.00 mm",  # 摘录新图纸的真实尺寸
                "inspection_method": "待定",  # 原文未指明量检具
                "acceptance_criteria": "Ø28.00 ~ Ø28.05 mm",
                "confidence": "高",
                "needs_human_review": False,
            }
        ], ensure_ascii=False)

    from src.cp_extractor.core.prompt_builder import PromptBuilder
    prompt_builder = PromptBuilder(user_id=101, db_path=test_db)
    pipe = StandardExtractorPipeline(
        llm_callable=mock_extract_new_part,
        user_id=101,
        prompt_builder=prompt_builder,
    )
    # 注入该专属 learner
    pipe.validator.feedback_learner = learner

    res = pipe.run("part_b.md", new_doc, manual_category="tolerance")
    assert len(res.check_items) == 1
    extracted_item = res.check_items[0]

    # 规范数值保持新图纸真实值，零污染！
    assert extracted_item.specification == "Ø28.00 +0.05/-0.00 mm"
    # 通用量检具、频次与控制方法成功享受历史沉淀经验自动回填！
    assert extracted_item.inspection_method == "高精度数显千分尺"
    assert extracted_item.sample_size_frequency == "5件/每班"
    assert extracted_item.control_method == "SPC控制图"
    assert extracted_item.reaction_plan == "停机隔离并通知班长"


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


def test_characteristic_list_pipeline_rule_fallback():
    """测试仅特性清单模式在离线/内置规则模式下的提取与去重"""
    from src.cp_extractor.core.extractor import StandardExtractorPipeline
    from src.cp_extractor.schemas.models import CharacteristicListPackage

    samples_dir = Path(__file__).resolve().parent / "benchmark_samples"
    sample_text = (samples_dir / "sample_tolerance_iso2768.md").read_text(encoding="utf-8")

    pipeline = StandardExtractorPipeline(llm_callable=None)
    pkg = pipeline.run_characteristic_list("sample_tolerance_iso2768.md", sample_text)
    assert isinstance(pkg, CharacteristicListPackage)
    assert pkg.standard_info.template_version == "char_list-v1.0"
    all_names = [c.characteristic_name for c in pkg.characteristics + pkg.pending_review_items]
    assert len(all_names) > 0


def test_characteristic_list_validator_and_page_resolution():
    """测试特性清单精简门禁：原文匹配、original_name 子串核对、页码定位"""
    from src.cp_extractor.core.validator import QualityGateValidator
    from src.cp_extractor.schemas.models import (
        CharacteristicEntry,
        CharacteristicListPackage,
        ConfidenceLevel,
        StandardInfo,
    )

    doc_text = """
    <!-- 第 1 页 -->
    3.1 屈服强度 ReH 应不小于 420 MPa。
    <!-- 第 2 页 -->
    3.2 表面粗糙度 Ra 控制在 1.6 μm。
    """
    item1 = CharacteristicEntry(
        characteristic_name="屈服强度",
        original_name="屈服强度 ReH",
        standard_clauses=["3.1"],
        source_quote="屈服强度 ReH 应不小于 420 MPa",
        confidence=ConfidenceLevel.HIGH,
    )
    # 原文名称不在引用中，触发门禁
    item2 = CharacteristicEntry(
        characteristic_name="抗拉强度",
        original_name="抗拉强度 Rm",
        standard_clauses=["3.2"],
        source_quote="表面粗糙度 Ra 控制在 1.6 μm",
        confidence=ConfidenceLevel.HIGH,
    )
    pkg = CharacteristicListPackage(
        standard_info=StandardInfo(file_name="test.pdf", standard_category="材料标准"),
        characteristics=[item1, item2],
    )
    val = QualityGateValidator()
    validated = val.validate_characteristic_list(pkg, source_full_text=doc_text)

    # item1 应该通过，且 source_page 解析为 1
    passed = [c for c in validated.characteristics if c.characteristic_name == "屈服强度"]
    assert len(passed) == 1
    assert passed[0].source_page == 1

    # item2 因为 original_name 不在 source_quote 中，应该进入 pending_review_items
    pending = [c for c in validated.pending_review_items if c.characteristic_name == "抗拉强度"]
    assert len(pending) == 1
    assert any("抗拉强度" in err for err in validated.validation_report.name_not_in_quote)


def test_characteristic_list_excel_export(tmp_path):
    """测试特性清单 Excel 导出功能"""
    from src.cp_extractor.core.mapper import CharacteristicListExcelMapper
    from src.cp_extractor.schemas.models import (
        CharacteristicEntry,
        CharacteristicListPackage,
        ConfidenceLevel,
        StandardInfo,
    )

    pkg = CharacteristicListPackage(
        standard_info=StandardInfo(file_name="test.pdf", standard_category="公差/尺寸标准"),
        characteristics=[
            CharacteristicEntry(
                characteristic_name="孔径公差",
                original_name="孔径",
                standard_clauses=["3.1", "表1"],
                source_quote="孔径控制在 Ø10 ± 0.05",
                source_page=2,
                is_mandatory=True,
                confidence=ConfidenceLevel.HIGH,
            )
        ],
    )
    excel_path = tmp_path / "test_char_list.xlsx"
    mapper = CharacteristicListExcelMapper()
    res_path = mapper.export(pkg, excel_path)
    assert res_path.exists()
