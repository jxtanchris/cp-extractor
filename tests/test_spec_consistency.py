"""
Spec-to-Code 自动化契约一致性测试 (Living Specification Contract Tests)

目的：
防止 spec.md 与不断演进的 Python 代码产生“规格漂移 (Spec Drift)”。
一旦开发者或 AI 修改了 Pydantic 数据模型、枚举定义、模板文件或核心模块路径，
而未同步更新 spec.md（或反之），本测试将立即报错并指出具体不一致项。
"""

import json
import re
from pathlib import Path
from typing import Dict, Tuple

from src.cp_extractor.core.llm_client import LLM_PROVIDERS
from src.cp_extractor.core.prompt_builder import DEFAULT_TEMPLATES
from src.cp_extractor.schemas.models import (
    CharacteristicCategory,
    CheckItem,
    ConfidenceLevel,
    CoverageSummary,
    ExtractionPackage,
    StandardCategory,
    StandardInfo,
    ValidationReport,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = PROJECT_ROOT / "spec.md"


def _load_spec_text() -> str:
    assert SPEC_PATH.exists(), f"未找到规格说明书: {SPEC_PATH}"
    return SPEC_PATH.read_text(encoding="utf-8")


def _parse_section_6_1_check_item_table(spec_text: str) -> Dict[str, Tuple[str, bool, str]]:
    """
    解析 spec.md 第 6.1 节 CheckItem 字段表格。
    返回: { field_name: (field_type, is_required, description) }
    """
    sec_match = re.search(
        r"###\s+6\.1\s+检查项\s*JSON\s*字段.*?\n(.*?)(?=\n###\s+6\.2|\Z)",
        spec_text,
        re.DOTALL,
    )
    assert sec_match, "spec.md 中缺少 '### 6.1 检查项 JSON 字段' 章节"
    sec_body = sec_match.group(1)

    fields: Dict[str, Tuple[str, bool, str]] = {}
    for line in sec_body.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line or "字段" in line:
            continue
        cols = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        if len(cols) >= 4:
            raw_name = cols[0].strip("` ")
            raw_type = cols[1].replace(r"\|", "|").strip("` ")
            raw_req = cols[2].strip()
            assert raw_req in ("是", "否"), f"spec.md 6.1 表格字段 '{raw_name}' 必填列格式非法: {raw_req}"
            desc = cols[3].strip()
            fields[raw_name] = (raw_type, raw_req == "是", desc)
    return fields


def test_spec_check_item_fields_match_pydantic_model():
    """契约 1：spec.md 6.1 节表格中的字段名与必填属性必须与 CheckItem 模型 100% 一致"""
    spec_text = _load_spec_text()
    spec_fields = _parse_section_6_1_check_item_table(spec_text)
    model_fields = CheckItem.model_fields

    spec_field_names = set(spec_fields.keys())
    code_field_names = set(model_fields.keys())

    missing_in_spec = code_field_names - spec_field_names
    extra_in_spec = spec_field_names - code_field_names

    assert not missing_in_spec and not extra_in_spec, (
        f"spec.md 第 6.1 节与 CheckItem 模型字段不一致！\n"
        f"  - 代码中有但 spec.md 缺失: {sorted(missing_in_spec)}\n"
        f"  - spec.md 中有但代码已移除: {sorted(extra_in_spec)}"
    )

    # 校验每个字段的必填性（is_required）是否与 spec.md 一致
    for field_name, field_info in model_fields.items():
        _, spec_required, _ = spec_fields[field_name]
        code_required = field_info.is_required()
        assert spec_required == code_required, (
            f"字段 '{field_name}' 必填属性不一致：spec.md 为 {spec_required}，CheckItem 模型为 {code_required}"
        )


def test_spec_extraction_package_json_matches_pydantic_models():
    """契约 2：spec.md 6.2 节 JSON 示例结构必须与 ExtractionPackage 及其子模型字段 100% 对齐"""
    spec_text = _load_spec_text()
    sec_match = re.search(
        r"###\s+6\.2\s+输出包结构.*?\n```json\s*(.*?)\s*```",
        spec_text,
        re.DOTALL,
    )
    assert sec_match, "spec.md 第 6.2 节中未找到有效的 ```json 代码块"

    raw_json = sec_match.group(1)
    pkg_dict = json.loads(raw_json)

    assert set(pkg_dict.keys()) == set(ExtractionPackage.model_fields.keys()), (
        f"spec.md 6.2 顶层字段与 ExtractionPackage 不一致："
        f"spec={set(pkg_dict.keys())} vs code={set(ExtractionPackage.model_fields.keys())}"
    )
    assert set(pkg_dict["standard_info"].keys()) == set(StandardInfo.model_fields.keys()), (
        f"spec.md 6.2 standard_info 字段与 StandardInfo 不一致："
        f"spec={set(pkg_dict['standard_info'].keys())} vs code={set(StandardInfo.model_fields.keys())}"
    )
    assert set(pkg_dict["coverage_summary"].keys()) == set(CoverageSummary.model_fields.keys()), (
        f"spec.md 6.2 coverage_summary 字段与 CoverageSummary 不一致："
        f"spec={set(pkg_dict['coverage_summary'].keys())} vs code={set(CoverageSummary.model_fields.keys())}"
    )
    assert set(pkg_dict["validation_report"].keys()) == set(ValidationReport.model_fields.keys()), (
        f"spec.md 6.2 validation_report 字段与 ValidationReport 不一致："
        f"spec={set(pkg_dict['validation_report'].keys())} vs code={set(ValidationReport.model_fields.keys())}"
    )

    # 验证该 JSON 示例本身可以直接通过 Pydantic 严格校验
    validated = ExtractionPackage.model_validate(pkg_dict)
    assert validated.standard_info.file_name == pkg_dict["standard_info"]["file_name"]


def test_spec_enums_match_codebase():
    """契约 3：代码中的核心枚举值（特性类别、置信度、标准分类）必须在 spec.md 中完整声明"""
    spec_text = _load_spec_text()

    for cat in CharacteristicCategory:
        assert cat.value in spec_text, f"CharacteristicCategory 枚举值 '{cat.value}' 未在 spec.md 中声明"

    for conf in ConfidenceLevel:
        assert conf.value in spec_text, f"ConfidenceLevel 枚举值 '{conf.value}' 未在 spec.md 中声明"

    for std_cat in StandardCategory:
        assert std_cat.value in spec_text, f"StandardCategory 枚举值 '{std_cat.value}' 未在 spec.md 中声明"


def test_spec_referenced_files_and_templates_exist():
    """契约 4：spec.md 中引用的所有源码路径 (src/...) 与模板路径 (templates/...) 必须真实存在"""
    spec_text = _load_spec_text()

    referenced_paths = set(
        re.findall(r"`((?:src/cp_extractor|templates)/[A-Za-z0-9_./-]+\.(?:py|md|html))`", spec_text)
    )
    assert referenced_paths, "spec.md 中未检测到任何源码或模板路径引用"

    for rel_path in sorted(referenced_paths):
        full_path = PROJECT_ROOT / rel_path
        assert full_path.exists(), f"spec.md 引用了不存在的文件路径: {rel_path}"

    # 验证 PromptBuilder 中的所有默认可编辑模板均已在 spec.md 第 5.1 节登记
    for tpl_key, tpl_info in DEFAULT_TEMPLATES.items():
        expected_rel = f"templates/{tpl_info['rel_path']}"
        assert expected_rel in referenced_paths, (
            f"PromptBuilder 中的模板 '{tpl_key}' ({expected_rel}) 未在 spec.md 中登记"
        )

    # 验证 LLM_PROVIDERS 中的所有大模型服务商标识均已在 spec.md 中登记
    for provider_key in LLM_PROVIDERS.keys():
        assert f"`{provider_key}`" in spec_text, (
            f"LLM_PROVIDERS 中的大模型服务商 '{provider_key}' 未在 spec.md 中声明"
        )

