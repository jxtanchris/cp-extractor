"""
四类汽车标准基准样本实测运行脚本
执行全链路：读取样本 -> 自动分类 -> 提示词组装 -> 提取 -> 质量门禁校验 -> 导出 JSON 与控制计划 Excel
"""

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.cp_extractor.core.extractor import StandardExtractorPipeline
from src.cp_extractor.core.mapper import ControlPlanExcelMapper


def main():
    root_dir = Path(__file__).resolve().parent.parent
    samples_dir = root_dir / "tests" / "benchmark_samples"
    output_dir = root_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    pipeline = StandardExtractorPipeline()
    mapper = ControlPlanExcelMapper()

    sample_files = sorted(samples_dir.glob("*.md"))
    summary_rows = []

    print("=" * 80)
    print("开始执行【标准→控制计划检查项提取系统】四类基准样本实测")
    print("=" * 80)

    for sample_path in sample_files:
        content = sample_path.read_text(encoding="utf-8")
        package = pipeline.run(file_name=sample_path.name, content=content)

        # 导出结构化 JSON
        json_path = output_dir / f"{sample_path.stem}_output.json"
        json_path.write_text(
            json.dumps(package.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 导出控制计划 Excel
        excel_path = output_dir / f"{sample_path.stem}_control_plan.xlsx"
        mapper.export(package, excel_path)

        cov = package.coverage_summary
        summary_rows.append({
            "file": sample_path.name,
            "category": package.standard_info.standard_category,
            "clauses": cov.total_clauses_scanned,
            "extracted": cov.check_items_extracted,
            "passed": len(package.check_items),
            "pending": len(package.pending_review_items),
            "high_conf": cov.high_confidence,
            "low_conf": cov.low_confidence,
            "json_out": json_path.name,
            "excel_out": excel_path.name,
        })

        print(f"\n[样本] {sample_path.name}")
        print(f"  ├─ 自动识别类别: {package.standard_info.standard_category} (模板 {package.standard_info.template_version})")
        print(f"  ├─ 扫描条款总数: {cov.total_clauses_scanned}")
        print(f"  ├─ 提取检查项数: {cov.check_items_extracted} (高置信度: {cov.high_confidence}, 低置信度: {cov.low_confidence})")
        print(f"  ├─ 正式准入项数: {len(package.check_items)} 项")
        print(f"  ├─ 门禁拦截待审: {len(package.pending_review_items)} 项")
        for p_item in package.pending_review_items:
            print(f"  │    └─ [待人工确认] 条款 {p_item.standard_clause} | {p_item.characteristic_name}: {p_item.notes}")
        print(f"  └─ 产物已生成  : output/{json_path.name} , output/{excel_path.name}")

    print("\n" + "=" * 80)
    print("全部 4 类基准样本实测完成！")
    print("=" * 80)


if __name__ == "__main__":
    main()
