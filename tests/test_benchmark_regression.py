"""
四类标准基准样本回归测试套件
严格验证 spec.md 第 11 节全部验收标准。
"""

import re
from pathlib import Path
import pytest

from src.cp_extractor.core.extractor import StandardExtractorPipeline
from src.cp_extractor.core.mapper import ControlPlanExcelMapper
from src.cp_extractor.schemas.models import ConfidenceLevel, StandardCategory

SAMPLES_DIR = Path(__file__).resolve().parent / "benchmark_samples"


@pytest.mark.parametrize(
    "sample_file,expected_category,expected_valid_count,expected_pending_count",
    [
        ("sample_tolerance_iso2768.md", StandardCategory.TOLERANCE, 10, 1),
        ("sample_material_qste420tm.md", StandardCategory.MATERIAL, 11, 1),
        ("sample_process_carburizing.md", StandardCategory.PROCESS, 8, 1),
        ("sample_appearance_coating.md", StandardCategory.APPEARANCE_PERFORMANCE, 7, 0),
    ],
)
def test_four_categories_benchmark_regression(
    sample_file: str,
    expected_category: StandardCategory,
    expected_valid_count: int,
    expected_pending_count: int,
    tmp_path: Path,
):
    """验证四类标准样本端到端提取、分类、溯源、门禁分流与 Excel 导出"""
    sample_path = SAMPLES_DIR / sample_file
    assert sample_path.exists(), f"样本文件不存在: {sample_path}"
    content = sample_path.read_text(encoding="utf-8")

    pipeline = StandardExtractorPipeline()
    package = pipeline.run(file_name=sample_file, content=content)

    # 1. 验证自动类别识别准确
    assert package.standard_info.standard_category == expected_category.value

    # 2. 验证提取数量与门禁分流数量
    assert len(package.check_items) == expected_valid_count
    assert len(package.pending_review_items) == expected_pending_count

    all_items = package.check_items + package.pending_review_items
    assert package.coverage_summary.check_items_extracted == len(all_items)
    assert package.coverage_summary.total_clauses_scanned > 0

    # 3. 验收标准：100% 可溯源性 & 零编造（原文引用必须在原标准文本中完全匹配）
    clean_source = re.sub(r"\s+", "", content)
    for item in all_items:
        assert item.standard_clause.strip() != ""
        assert item.source_quote.strip() != ""
        clean_quote = re.sub(r"\s+", "", item.source_quote)
        assert clean_quote in clean_source, f"发现非原文引用: {item.source_quote}"

    # 4. 验收标准：低置信度项 100% 标记 needs_human_review=True 且进入 pending_review_items
    for item in all_items:
        if item.confidence == ConfidenceLevel.LOW:
            assert item.needs_human_review is True
            assert item in package.pending_review_items

    # 5. 验收标准：正式通过项不得有未解决的校验错误
    assert len(package.validation_report.missing_clause_or_quote) == 0
    assert len(package.validation_report.method_misclassified_as_criteria) == 0

    # 6. 验收标准：可导出为控制计划 Excel
    mapper = ControlPlanExcelMapper()
    excel_path = tmp_path / f"{sample_path.stem}.xlsx"
    exported = mapper.export(package, excel_path)
    assert exported.exists()


def test_category_specific_plugin_constraints():
    """验证各插件专属约束规则是否生效"""
    pipeline = StandardExtractorPipeline()

    # 公差标准：GD&T 符号保留原符号（⏥, ⊥, ⌖, Ⓜ）
    tol_text = (SAMPLES_DIR / "sample_tolerance_iso2768.md").read_text(encoding="utf-8")
    tol_pkg = pipeline.run("sample_tolerance_iso2768.md", tol_text)
    specs = " ".join(i.specification for i in tol_pkg.check_items)
    for sym in ["⏥", "⊥", "⌖", "Ⓜ"]:
        assert sym in specs, f"GD&T 符号 {sym} 未保留"

    # 工艺标准：剔除设备管理要求（条款 2.1 九点测温、2.2 自动吹扫装置不得混入控制计划检查项）
    proc_text = (SAMPLES_DIR / "sample_process_carburizing.md").read_text(encoding="utf-8")
    proc_pkg = pipeline.run("sample_process_carburizing.md", proc_text)
    clauses = [i.standard_clause for i in proc_pkg.check_items + proc_pkg.pending_review_items]
    assert "2.1" not in clauses, "设备校验条款 2.1 不应作为过程控制参数提取"
    assert "2.2" not in clauses, "设备配置条款 2.2 不应作为过程控制参数提取"
