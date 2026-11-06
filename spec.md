# 标准→控制计划检查项提取系统 规格说明书 (Living Specification)

版本：v1.4
日期：2026-11-06
状态：已生效（与代码库通过 `tests/test_spec_consistency.py` 自动化契约绑定）

---

## 1. 背景与目标

### 1.1 背景
汽车行业质量工程师（IATF 16949）需要从各类技术标准（公差、材料、工艺、外观/性能等）中提取“可检查、可验证”的关键质量特性（CTQ），转化为控制计划（Control Plan）中的检查项。人工提取耗时且易遗漏，通用 AI 提示词又容易因标准类别差异导致输出不规范、篡改单边公差或不可审核。

### 1.2 目标
构建一套基于 LLM/结构化规则的智能提取系统（**CP-Extractor**），通过 **“公共基础模板 + 四类专用插件 + 双层后处理质量门禁 + 二次语义自纠错 + 人工确认复核工作台”** 的架构，实现：
- 从多类标准（PDF、扫描件、Word、Markdown、粘贴截图）中稳定提取检查项；
- 输出统一结构化 JSON（严格契合 Pydantic V2 数据契约），可直接映射导出带页码跳转超链接的控制计划 Excel（`.xlsx`）；
- 每条检查项可溯源（精确到标准条款号与 PDF 页码 `#page=N`）、零编造、可提交审核；
- 支持本地 FastAPI 可视化 Web 工作台与外部 RAG/FastGPT 工作流落地。

### 1.3 非目标
- 不替代质量工程师的标准解释与最终提审签字责任；
- 不追求全自动无人审核（低置信度与门禁拦截项必须人工确认）；
- 首期不覆盖复杂 3D CAD 数模直接解析（列为后续扩展）。

---

## 2. 术语定义

| 术语 | 定义 |
|---|---|
| 检查项 (`CheckItem`) | 控制计划中一行可检查、可验证的要求，含特性名称、特性类别、规范/公差、检查方法、判定准则、条款号、PDF页码、原文引用等 |
| 产品关键特性 (Product CTQ) | 图纸或标准中的线性/角度尺寸、单双边公差、14 类 GD&T 控制框、表面粗糙度、材料理化及外观性能要求 |
| 过程关键特性 (Process CTQ) | 生产制造过程中的特殊工艺窗口，如热处理温度/时间/碳势、焊接规范、涂装参数、拧紧扭矩等 |
| 公共基础模板 | 所有标准类别共用的提示词骨架（`templates/base_template.md`），定义顺序锚定规则、输出 Schema 与全局判定准则 |
| 类别专用插件 | 追加到基础模板后的类别特定提取指令与 Few-Shot 正反例（`templates/plugins/*.md`） |
| 后处理校验与自纠错 | 结合确定性规则门禁（`QualityGateValidator`）与二次语义打回重提（`refine_items_with_feedback`）的双层校验机制 |
| 人工确认门禁 | 低置信度、参考尺寸、原文未完全匹配或方法判据混淆项必须进入 `pending_review_items`，经人工复核后才能准入控制计划 |
| 校验词库 (`LexiconDB`) | 存储于 `data/lexicon.db` 的 SQLite 多语言（中/英/日/德/符号）量检具、物理单位与合格判据特征词库 |

---

## 3. 系统架构

### 3.1 总体流程

```text
标准文档上传 (.pdf / .docx / .md / .txt) 或剪贴板截图粘贴 (Cmd+V)
↓
多格式文档解析（pypdf 布局表格还原 / python-docx 表格转换 / Apple Vision & Tesseract OCR）
↓
标准类别识别（用户手动指定 或 StandardClassifier 自动分类）+ 长文档按章节语义分块
↓
提示词组装（PromptBuilder = 公共基础模板 + 类别专用插件 + 顺序锚定 Schema）
↓
LLM 提取引擎（DeepSeek temperature=0.0 确定性提取 / 结构化基准提取）→ 跨块去重合并
↓
后处理质量门禁（QualityGateValidator + SQLite 多语言词库 + PDF 页码精确定位）
↓
[若触发硬性偏差] → 二次语义自纠错闭环（refine_items_with_feedback 定向重提并二次过检）
↓
输出封装（ExtractionPackage）：正式检查项清单 + 待确认清单 + 覆盖率摘要 + 校验报告
↓
Web 工作台人工审核（支持条款号一键跳转 PDF 对应页码 #page=N、在线修订准入或驳回）
↓
字段映射导出 → 控制计划标准格式 Excel (.xlsx，自带 PDF 页码超链接) & JSON
```

### 3.2 模块职责与代码映射

| 模块 | 代码路径 | 核心职责 |
|---|---|---|
| 数据契约模块 | `src/cp_extractor/schemas/models.py` | 定义 `CheckItem`、`StandardInfo`、`CoverageSummary`、`ValidationReport`、`ExtractionPackage` 等 Pydantic 模型与枚举 |
| 文档解析模块 | `src/cp_extractor/core/parser.py` | 将 PDF/Word/Markdown/截图转为结构化文本，还原跨列表格并注入 `<!-- 第 X 页 -->` 分页标记 |
| 类别识别模块 | `src/cp_extractor/core/classifier.py` | 识别标准类别（`公差/尺寸标准`、`材料标准`、`工艺标准`、`外观/性能标准`），路由至对应插件 |
| 提示词组装模块 | `src/cp_extractor/core/prompt_builder.py` | 组装基础模板与类别插件，支持 Web 端在线编辑、版本解析与一键恢复出厂默认 |
| 核心提取流水线 | `src/cp_extractor/core/extractor.py` | 负责长文档按章节语义分块、页码上下文承接、跨块去重、门禁调用与二次自纠错编排 |
| 多模型客户端模块 | `src/cp_extractor/core/llm_client.py` | 封装通用多后端大模型客户端（`LLMClient`，支持 `deepseek`、`qwen`、`zhipu`、`kimi`、`siliconflow`、`openai`、`ollama`、`custom` 一键切换、连通性测试、`.env` 持久化与 `refine_items_with_feedback` 打回重提） |
| 质量门禁校验器 | `src/cp_extractor/core/validator.py` | 执行 6 项确定性门禁校验、原文防幻觉核对、PDF 页码反查（`resolve_source_page`）、自学习规则自动放行/剔除与待确认分流 |
| 自学习规则引擎 | `src/cp_extractor/core/feedback_learner.py` | 基于 SQLite (`learned_rules` 表) 观察记录工程师手工准入/驳回决策，累计达阈值 $N$ 次后自动更改门禁直通规则、动态注入提示词指令并扩充词库 |
| 多语言校验词库 | `src/cp_extractor/core/lexicon_db.py` | 基于 SQLite (`data/lexicon.db`) 管理中/英/日/德多语种量检具（`method`）、单位（`unit`）、判据（`criteria`）词条 |
| 输出映射导出器 | `src/cp_extractor/core/mapper.py` | 将审核后数据导出为控制计划 Excel（`.xlsx`），自动写入条款 `#page=N` 蓝色超链接与多工作表报告 |
| Web 服务与工作台 | `src/cp_extractor/api/app.py` | 提供 REST API、源 PDF 在线预览、人工确认门禁复核、提示词配置中心与词库管理界面 |

---

## 4. 标准类别与提取重点

| 标准类别 (`StandardCategory`) | 插件标识 | 检查项重点 | 提取难点与防控策略 |
|---|---|---|---|
| `公差/尺寸标准` | `tolerance` | 尺寸公差、14 类 GD&T 控制框、基准、MMC/LMC、粗糙度、未注公差表 | 单边公差（`+0.05/-0.00`）严禁改写为 `±0.05`；过滤括号参考尺寸 `(120)`；未注公差表逐行独立拆分 |
| `材料标准` | `material` | 化学成分、抗拉强度、屈服强度、延伸率、冷弯、硬度、金相组织 | 化学元素逐项独立拆分；严格区分“试验方法”与“合格判据”；保留试验温度/时长条件 |
| `工艺标准` | `process` | 渗碳/淬火/回火温度、保温时间、碳势、搅拌转速、焊接/涂装过程窗口 | 特性名称标注 `【适用工序】`；严格剔除“设备定期校验/维护要求”（如九点炉温均匀性测试） |
| `外观/性能标准` | `appearance_performance` | 表面缺陷限度、目视光源/视距、色差光泽度、膜厚、附着力、耐盐雾 | 定性描述转化为可量化 `0 缺陷` 判据；保留观测条件（如 `D65 光源 ≥1000 lx，视距 500mm`）；区分 `外观` 与 `性能` |

---

## 5. 提示词模板规范（单一事实源引用）

> **单一事实源原则（Single Source of Truth）**：为防止规格文档与运行时提示词发生双写漂移，提示词正文统一存储于 `templates/` 目录下，由 `PromptBuilder` 动态加载并支持 Web 端在线微调与一键还原。

### 5.1 模板文件清单

| 模板标识 | 文件路径 | 核心职责与约束规则 |
|---|---|---|
| `base` | `templates/base_template.md` | **公共基础模板**：定义 Product/Process CTQ 双维扫描、只提取受控强制要求、排除括号参考尺寸、**顺序锚定生成（先输出 `standard_clause` 与 `source_quote` 锚定原文，再推导参数）**、建议性条款强制标记低置信度与人工确认 |
| `tolerance` | `templates/plugins/tolerance.md` | **公差/尺寸标准插件 (v1.2)**：涵盖 ASME Y14.5 / ISO 1101 全部 14 类 GD&T 几何特征符号、单边公差逐字保真、括号参考尺寸过滤、未注公差表逐行提取及 Few-Shot 正反例 |
| `material` | `templates/plugins/material.md` | **材料标准插件 (v1.1)**：化学成分按元素逐条拆分、试验方法与合格判据分离、保留试验环境条件及 Few-Shot 正反例 |
| `process` | `templates/plugins/process.md` | **工艺标准插件 (v1.1)**：过程特性标注工序前缀、剔除设备定期校验/工装维护条款及 Few-Shot 正反例 |
| `appearance_performance` | `templates/plugins/appearance_performance.md` | **外观/性能标准插件 (v1.1)**：定性外观要求转化为定量零缺陷判据、保留光源视距与盐雾试验条件及 Few-Shot 正反例 |
| `validation` | `templates/validation_template.md` | **后处理校验与二次自纠错模板**：指导 LLM 对门禁拦截项（缺失条款/原文、单位不一致、方法误写为判据、单边公差篡改）进行定向修正 |
| `gdt_reference` | `templates/references/gdt_symbols.md` | **GD&T 符号标准参考库**：定义直线度 `⎯`、平面度 `⏥`、圆度 `⌯`、圆柱度 `⌭`、线轮廓度 `⌒`、面轮廓度 `⌓`、垂直度 `⊥`、平行度 `∥`、倾斜度 `∠`、位置度 `⌖`、同轴度 `◎`、对称度 `≡`、圆跳动 `↗`、全跳动 `⌕` 共 14 类控制框解析规范 |

---

## 6. 输出 Schema 规范

> 本节定义的字段表格与 JSON 结构由 `tests/test_spec_consistency.py` 自动解析并与 `src/cp_extractor/schemas/models.py` 进行双向契约校验。任何模型字段增删必须同步更新本节。

### 6.1 检查项 JSON 字段 (`CheckItem`)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `characteristic_name` | `string` | 是 | 特性名称（控制计划术语，GD&T 需含基准，工艺参数需含工序） |
| `characteristic_category` | `enum` | 是 | 特性类别枚举：`尺寸` / `材料` / `工艺参数` / `外观` / `性能` |
| `specification` | `string` | 是 | 规范或公差，逐字保留原文数值、单位、单双边公差符号与判定描述 |
| `inspection_method` | `string` | 是 | 检查方法（量检具、仪器或试验标准，如卡尺、三坐标测量机、拉伸试验） |
| `acceptance_criteria` | `string` | 是 | 判定准则，合格/不合格的明确数值边界或状态条件 |
| `standard_clause` | `string` | 是 | 标准条款号或章节号（如 `3.1`、`4.2`） |
| `source_page` | `integer \| null` | 否 | 标准条款或原文所在的 PDF 页码（从 1 开始，用于 `#page=N` 超链接跳转） |
| `source_quote` | `string` | 是 | 原文引用逐字摘录（防幻觉溯源锚点） |
| `confidence` | `enum` | 是 | 提取置信度枚举：`高` / `中` / `低` |
| `needs_human_review` | `boolean` | 是 | 是否需人工确认（当 `confidence` 为 `低` 或触发质量门禁时强制为 `true`） |
| `notes` | `string \| null` | 否 | 备注说明或门禁拦截提示 |

### 6.2 输出包结构 (`ExtractionPackage`)

```json
{
  "standard_info": {
    "file_name": "example_standard.pdf",
    "standard_category": "公差/尺寸标准",
    "template_version": "v1.4",
    "source_file_url": "/api/files/example_standard.pdf"
  },
  "check_items": [],
  "pending_review_items": [],
  "coverage_summary": {
    "total_clauses_scanned": 0,
    "check_items_extracted": 0,
    "high_confidence": 0,
    "medium_confidence": 0,
    "low_confidence": 0
  },
  "validation_report": {
    "missing_clause_or_quote": [],
    "unit_inconsistency": [],
    "method_misclassified_as_criteria": [],
    "unmarked_low_confidence": [],
    "suggestions": []
  }
}
```

---

## 7. 质量门禁（提审结果必须满足）

系统通过 `Pydantic 模型校验器` + `QualityGateValidator` 实施 **6 项确定性质量门禁** 与 **二次自纠错闭环**：

1. **条款与原文逐字溯源门禁（防幻觉）**：
   - 每条检查项必须包含有效 `standard_clause` 与 `source_quote`；
   - 校验器自动去除空白符比对 `source_quote` 是否真实存在于原文本中，若失配则记入 `missing_clause_or_quote` 并强制分流至 `pending_review_items`。
2. **低置信度 100% 人工确认门禁**：
   - 凡 `confidence == "低"` 的检查项，无论 LLM 原始输出如何，Pydantic 模型层与校验器层双重强制设置 `needs_human_review = true`。
3. **试验方法与判定准则语义防混淆门禁**：
   - 结合 SQLite 多语言词库（`LexiconDB`），若 `acceptance_criteria` 中出现量检具/试验仪器词条（`method`）且缺乏合格判据特征词（`criteria`），记入 `method_misclassified_as_criteria` 并拦截待审。
4. **数值单位完整性校验**：
   - 对含数值的 `specification` 校验是否具备物理单位、GD&T 符号或标准等级说明，缺失时记入 `unit_inconsistency` 告警。
5. **CTQ 括号参考尺寸拦截门禁**：
   - 自动检测形如 `(120)`、`(Ø62)` 的圆括号参考尺寸（`REF_DIM_PATTERN`），一旦误提则自动拦截至 `pending_review_items` 并提示工程师剔除。
6. **CTQ 单边公差防篡改门禁**：
   - 当 `source_quote` 含有单边公差（如 `+0.05/-0.00`）而 `specification` 被改写为对称公差 `±` 时，立即判定违反逐字保真原则并拦截报警。
7. **二次语义自纠错闭环 (`refine_items_with_feedback`)**：
   - 当首次提取触发硬性偏差（条款/原文失配、单边公差篡改、方法与判据混淆）时，流水线自动调用 `validation_template.md` 携带错误报告打回 LLM 定向重提一次，二次过检仍未通过的项安全保留在 `pending_review_items` 交由人工复核。
8. **人工复核反馈自学习门禁直通/自动剔除 (`FeedbackRuleLearner`)**：
   - 系统持续记录工程师在 `pending_review_items` 上的手工确认准入（`approve`）与驳回剔除（`reject`）行为；当同一检查项名称累计确认/驳回达到设定阈值 $N$ 次（默认 `3` 次）且方向一致时，自动晋升为激活的自学习规则，在后续提取中执行门禁自动直通（含检查方法与判据回填）或自动静默剔除，并同步向提取 Prompt 注入历史经验指令。

---

## 8. 落地方式与工程实现

1. **文档输入与增强解析**：支持上传 `.pdf`（原生文本层与扫描件 OCR）、`.docx`（自动还原 Word 表格）、`.md`、`.txt`，或直接在 Web 界面按 `Cmd+V` / `Ctrl+V` 粘贴标准截图（调用 macOS Apple Vision / Tesseract OCR）。
2. **长文档语义分块**：按章节标题与主条款边界智能切分（默认阈值 `3500` 字符），跨块自动携带文档头部与最新 PDF 页码标记 `<!-- 第 X 页 -->`。
3. **提示词动态组装与在线治理**：按标准类别拼接 `base_template.md` 与对应插件，支持在 Web 工作台实时修改提示词与管理 SQLite 多语言校验词库。
4. **条款页码定位与一键跳转**：自动计算每条检查项对应的 `source_page`，在 Web 界面渲染为可点击胶囊（`📄 条款号 (P.页码) ↗`），并在导出的 `.xlsx` 报表中同步生成 `#page=N` 超链接。
5. **人工确认门禁复核工作台**：工程师可在 Web 端对 `pending_review_items` 在线编辑修正、确认准入（移入 `check_items`）或驳回剔除。
6. **多后端大模型一键切换与参数自适应**：Web 端与 API 支持在 8 类云端/本地大模型服务商间一键切换，支持调节采样温度 `temperature`（`0.0 ~ 2.0`），并针对 `kimi-k2.6` 等固定温度模型自动容错适配。
7. **自学习规则中心可视化治理**：提供 `🧠 自学习规则中心` 弹窗与 REST API，支持实时调整自学习触发阈值 $N$、暂停/恢复单条自学习规则或一键清空重置。

---

## 9. 版本管理与契约治理

### 9.1 版本号与治理规则
- **版本号规则**：`主版本.次版本`（主版本变更核心数据 Schema 结构，次版本新增系统模块、门禁规则或提取指令）。
- **回归测试要求**：四大类标准在 `tests/benchmark_samples/` 下各保留至少 1 份基准样本，任何代码或模板变更后必须通过 `pytest` 全量回归测试。
- **Spec 与代码自动化一致性契约**：通过 `tests/test_spec_consistency.py` 强制校验本文件第 3、5、6 节与代码实现的一致性。

### 9.2 版本变更记录 (Changelog)

| 版本 | 日期 | 状态 | 变更说明 |
|---|---|---|---|
| `v1.0` | 2026-11-05 | 归档 | 初始规格说明书草案：确立公共基础模板 + 四类专用插件 + 后处理校验 + 人工确认门禁架构与基础 JSON Schema |
| `v1.1` | 2026-11-05 | 归档 | 新增 Product/Process CTQ 双维体系、14 类 GD&T 符号库、单边公差防篡改与括号参考尺寸拦截门禁、二次语义自纠错闭环及长文档章节语义分块 |
| `v1.2` | 2026-11-06 | 归档 | 1. `CheckItem` 新增 `source_page`，`StandardInfo` 新增 `source_file_url`，支持 Web 与 Excel 条款 `#page=N` 超链接跳转；<br>2. 新增 SQLite 多语言词库 (`LexiconDB`) 与剪贴板截图 Vision OCR 解析；<br>3. 建立 `tests/test_spec_consistency.py` 自动化契约测试，实现 `spec.md` 与代码库双向绑定 |
| `v1.3` | 2026-11-06 | 归档 | 新增多后端大模型一键切换架构（`LLMClient` / `LLM_PROVIDERS`），内置支持 `deepseek`（DeepSeek）、`qwen`（通义千问）、`zhipu`（智谱 GLM）、`kimi`（月之暗面）、`siliconflow`（硅基流动）、`openai`（OpenAI）、`ollama`（本地私有化免 Key）及 `custom`（自定义接口），支持按厂商独立保存配置、采样温度 (`temperature`) 自适应调节、在线连通性测试与一键同步写入服务端 `.env` |
| `v1.4` | 2026-11-06 | 已生效 | 新增人工复核反馈自学习引擎（`FeedbackRuleLearner`，`src/cp_extractor/core/feedback_learner.py`）：自动观察工程师在待确认项上的手工准入/驳回操作，累计达到可配置阈值 $N$ 次后，自动生成门禁直通/剔除规则、自动回填检查方法与判据、动态注入提示词并扩充 SQLite 量检具词库 |

---

## 10. 后续扩展

- 图纸级复杂 2D/3D CAD 与 GD&T 多模态视觉深度解析；
- 与 PFMEA、作业指导书（SOP/WI）交叉审查系统联动；
- 对接企业 PLM/QMS 系统，自动同步 APQP/PPAP 控制计划版本；
- 提取质量指标看板（准确率、召回率、门禁拦截率、人工修改率统计）。

---

## 11. 验收标准

| 验收项 | 验收标准 | 验证方式 |
|---|---|---|
| 输出格式契约 | 100% 符合第 6 节 JSON Schema，且与 `models.py` 字段零偏差 | `pytest tests/test_spec_consistency.py` |
| 可溯源性 | 100% 准入检查项带标准条款号（`standard_clause`）、原文引用（`source_quote`）及 PDF 页码定位 | `pytest tests/test_extractor.py` |
| 不编造（保真度） | 原文引用 100% 匹配原文档，单边公差零篡改，括号参考尺寸 100% 拦截 | `QualityGateValidator` 单元测试 |
| 人工确认覆盖 | 低置信度项 100% 标记 `needs_human_review=true` 并分流至 `pending_review_items` | `CheckItem` 模型校验与回归测试 |
| 类别适配 | 四大类标准各至少 1 份基准样本 100% 通过端到端回归测试 | `pytest tests/test_benchmark_regression.py` |
| 提审可用性 | 输出可直接导出带页码超链接的 `.xlsx` 控制计划报表并通过 Web 工作台复核 | `pytest tests/test_api_and_deepseek.py` |

---

## 12. 风险与注意事项

1. **复杂跨页表格解析风险**：尽管已启用 `pypdf` 空间布局表格还原与章节分块，极复杂的合并单元格或异形跨页表格仍需人工抽检；
2. **试验方法与判定准则混淆**：LLM 可能将试验设备误填入判定准则，依赖 `QualityGateValidator` + `LexiconDB` 多语言词库拦截与二次自纠错；
3. **类别识别错配风险**：混合型标准可能导致自动分类偏差，Web 工作台与 API 均保留人工指定类别（`manual_category`）覆盖能力；
4. **人工审核不可省略**：系统旨在大幅提升提取效率与格式一致性，但最终工程解释与质量签字责任仍在质量工程师；
5. **规格与代码同步纪律**：任何对 `src/cp_extractor/schemas/models.py` 或 `templates/` 结构的修改，必须同步更新本 `spec.md` 并通过 `pytest` 契约测试。
