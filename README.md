# 标准→控制计划检查项提取系统 (CP-Extractor)

基于大语言模型（LLM）与 RAG 架构的汽车行业质量技术标准检查项智能提取系统。

---

## 1. 核心架构与设计思想

本系统专为汽车行业质量工程师打造，旨在将繁杂的技术标准（如公差、材料、工艺、外观/性能等）转化为控制计划（Control Plan）中结构化、可验证、可审核的检查项清单。

遵循五大设计原则：
1. **统一 Schema 规范**：输出标准化 JSON，包含特性名称、类别、规范/公差、检查方法、判定准则、条款号、原文引用、置信度及门禁标识。
2. **模块化插件提示词**：采用"公共基础模板 + 4类专用插件（公差/材料/工艺/外观性能）"组合模式。
3. **双轨质量校验**：代码规则硬性校验（字段、溯源、反幻觉文本匹配）+ 语义软性校验（试验方法与判定准则混淆拦截）。
4. **人工确认门禁**：低置信度项与校验 FAIL 项自动进入待复核清单，保障提审严谨性。
5. **多端落地**：原生支持 Python SDK、Excel 报表导出及 FastGPT 等工作流平台。

---

## 2. 项目目录结构

```text
CP/
├── spec.md                   # 规格说明书 (v1.0 草案)
├── pyproject.toml            # Python 依赖与工程配置 (uv 管理)
├── README.md                 # 项目说明文档
├── templates/                # 提示词模板与插件目录
│   ├── base_template.md      # 公共基础提取模板
│   ├── validation_template.md# 后处理校验模板
│   └── plugins/              # 四大类专用插件
│       ├── tolerance.md      # 公差/尺寸标准插件
│       ├── material.md       # 材料标准插件
│       ├── process.md        # 工艺标准插件
│       └── appearance_performance.md # 外观/性能标准插件
├── src/
│   └── cp_extractor/         # 核心代码包
│       ├── schemas/          # Pydantic V2 严格数据模型
│       │   └── models.py
│       └── core/             # 业务核心
│           ├── prompt_builder.py  # 提示词组合与插件管理器
│           ├── validator.py       # 质量门禁与反幻觉校验引擎
│           └── mapper.py          # 控制计划 Excel 导出映射器
├── tests/                    # 单元测试与基准测试
│   └── test_extractor.py
└── filemaker_script/         # FileMaker 脚本 XML 同步生成目录
```

---

## 3. 快速上手

### 环境要求
- Python >= 3.12
- 包管理器推荐：[uv](https://github.com/astral-sh/uv)

### 安装依赖与运行测试
```bash
# 运行单元测试
uv run pytest

# 运行代码覆盖率
uv run pytest --cov=src
```

### Python SDK 使用示例
```python
from cp_extractor.core.prompt_builder import PromptBuilder
from cp_extractor.core.validator import QualityGateValidator
from cp_extractor.core.mapper import ControlPlanExcelMapper
from cp_extractor.schemas.models import ExtractionPackage, StandardInfo, CheckItem, CharacteristicCategory, ConfidenceLevel

# 1. 组装提示词
builder = PromptBuilder()
prompt = builder.build_extraction_prompt(
    category_key="material", 
    context_text="待分析的标准章节正文..."
)

# 2. 假设 LLM 返回了 CheckItem 列表，构建抽取包
package = ExtractionPackage(
    standard_info=StandardInfo(file_name="GB_T_228.1.pdf", standard_category="材料标准"),
    check_items=[...],
)

# 3. 执行质量门禁校验（结合原文进行防编造防幻觉核对）
validator = QualityGateValidator()
verified_package = validator.validate_package(package, source_full_text="待分析的标准章节正文...")

# 4. 导出控制计划 Excel
mapper = ControlPlanExcelMapper()
mapper.export(verified_package, "output/control_plan_result.xlsx")
```
