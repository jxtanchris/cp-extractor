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
    assert "汽车行业质量工程师" in base
    
    # 4类插件装配
    categories = ["tolerance", "material", "process", "appearance_performance"]
    for cat in categories:
        prompt = builder.build_extraction_prompt(cat, context_text="示例标准文本")
        assert "示例标准文本" in prompt
        assert "输出要求" in prompt


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
