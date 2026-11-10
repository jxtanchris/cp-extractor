# 标准→控制计划检查项提取系统 规格说明书 (Living Specification)

版本：v2.1
日期：2026-11-10
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
| 用户认证与权限体系 (`AuthManager`) | 基于 SQLite (`data/app.db`)、PBKDF2-HMAC-SHA256 盐值密码散列与 RFC 7519 HS256 JWT Token 签发的工程师鉴权与管理员特权保护架构 |
| 提示词两级继承架构 | 用户个性化定制模板优先存储于 `user_templates` 表，未定制时自动回退至磁盘 `templates/` 系统默认模板（零污染官方文件并支持单键恢复出厂） |
| 文件存储沙箱与 IDOR 防护 | 用户上传的源文档与导出的产物物理隔离于 `output/users/{user_id}/` 目录，通过 Header 与 `?token=` 双通道鉴权，阻断跨用户水平越权访问与路径穿越攻击 |

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
| 类别识别模块 | `src/cp_extractor/core/classifier.py` | 识别标准主类别（`公差/尺寸标准`、`材料标准`、`工艺标准`、`外观/性能标准`），并支持长文档按章节**块级动态路由（Chunk-level Dynamic Routing）**与单块多插件融合（`classify_chunk_plugins`） |
| 提示词组装模块 | `src/cp_extractor/core/prompt_builder.py` | 组装基础模板与单/多类别插件，支持 Web 端在线编辑、版本解析与一键恢复出厂默认 |
| 核心提取流水线 | `src/cp_extractor/core/extractor.py` | 负责长文档按章节语义分块、块级插件动态路由、页码上下文承接、跨块去重、门禁调用与精准隔离+零丢失对账的二次自纠错编排 |
| 多模型客户端模块 | `src/cp_extractor/core/llm_client.py` | 封装通用多后端大模型客户端（`LLMClient`，支持 `deepseek`、`qwen`、`zhipu`、`kimi`、`siliconflow`、`openai`、`ollama`、`custom` 一键切换、连通性测试、`.env` 持久化与 `refine_items_with_feedback` 打回重提） |
| 质量门禁校验器 | `src/cp_extractor/core/validator.py` | 执行 6 项确定性门禁校验、原文防幻觉核对、PDF 页码反查（`resolve_source_page`）、硬门禁绝对优先的自学习接管与待确认分流 |
| 自学习规则引擎 | `src/cp_extractor/core/feedback_learner.py` | 基于 SQLite (`learned_rules` 表) 观察记录工程师手工准入/驳回决策，采用严格核心名称匹配，累计达阈值 $N$ 次后自动更改软门禁直通规则、回填 IATF 16949 扩展字段、动态注入提示词并扩充词库 |
| 多语言校验词库 | `src/cp_extractor/core/lexicon_db.py` | 基于 SQLite (`data/lexicon.db`) 管理中/英/日/德多语种量检具（`method`）、单位（`unit`）、判据（`criteria`）词条 |
| 输出映射导出器 | `src/cp_extractor/core/mapper.py` | 将审核后数据导出为 15 列 IATF 16949 控制计划 Excel（`.xlsx`），自动补全完整 HTTP 基地址并写入条款 `#page=N` 蓝色超链接与多工作表报告 |
| 用户认证与鉴权核心 | `src/cp_extractor/schemas/auth.py`, `src/cp_extractor/core/auth.py` | 提供 PBKDF2-HMAC-SHA256 密码加盐散列、HS256 JWT Token 签发与解析校验、个人密码自主安全修改、系统管理员用户浏览与审计、默认开箱管理员 `admin/admin`、非管理员 403 权限拦截与 401 强制登录保护 |
| 解析历史管理模块 | `src/cp_extractor/core/history.py` | 基于 SQLite (`extraction_history` 表) 记录各工程师提取历史、多租户隔离查询、系统管理员穿透审计指定用户历史、沙箱文件秒级重载回工作台与安全清理 |
| 存储沙箱与多租户隔离 | `src/cp_extractor/api/app.py` | 实现 `output/users/{user_id}/` 沙箱隔离存储、Path Traversal 路径穿越防护、IDOR 水平越权拦截、Header + URL `?token=` 双通道下载/预览支持与自学习经验按用户隔离 |
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
| `char_list` | `templates/characteristic_list_template.md` | **仅特性清单模板 (v1.0)**：专用于仅提取特性名称场景，统一内置四类标准的扫描清单，偏召回提取全部可检查特性名称（中文规范名 + 原文名称），不提取规范/公差、检查方法与判定准则 |
| `gdt_reference` | `templates/references/gdt_symbols.md` | **GD&T 符号标准参考库**：定义直线度 `⎯`、平面度 `⏥`、圆度 `⌯`、圆柱度 `⌭`、线轮廓度 `⌒`、面轮廓度 `⌓`、垂直度 `⊥`、平行度 `∥`、倾斜度 `∠`、位置度 `⌖`、同轴度 `◎`、对称度 `≡`、圆跳动 `↗`、全跳动 `⌕` 共 14 类控制框解析规范 |

---

## 6. 输出 Schema 规范

> 本节定义的字段表格与 JSON 结构由 `tests/test_spec_consistency.py` 自动解析并与 `src/cp_extractor/schemas/models.py` 进行双向契约校验。任何模型字段增删必须同步更新本节。

### 6.1 检查项 JSON 字段 (`CheckItem` v2.0 15字段 IATF 16949 标准契约)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `characteristic_name` | `string` | 是 | 特性名称（控制计划术语，GD&T 需含基准，工艺参数需含工序） |
| `characteristic_category` | `enum` | 是 | 特性类别枚举：`尺寸` / `材料` / `工艺参数` / `外观` / `性能` |
| `special_char_class` | `string \| null` | 否 | 特殊特性分类（如 `CC`、`SC`、`PTC`、`关键特性`、`重要特性`；仅在原文明确标注时提取，否则为 `null`） |
| `specification` | `string` | 是 | 规范或公差，逐字保留原文数值、单位、单双边公差符号与判定描述 |
| `inspection_method` | `string` | 是 | 检查方法（量检具、仪器或试验标准，如卡尺、三坐标测量机、拉伸试验） |
| `acceptance_criteria` | `string` | 是 | 判定准则，合格/不合格的明确数值边界或状态条件 |
| `sample_size_frequency` | `string \| null` | 否 | 样本容量与抽样频次（如 `100% 检验`、`5件/每班`、`每批首末件`；严守零编造，原文未提及时为 `null`） |
| `control_method` | `string \| null` | 否 | 控制方法（如 `SPC控制图`、`首末件记录表`、`防错点检表`、`在线连续监控记录`；原文未提及时为 `null`） |
| `reaction_plan` | `string \| null` | 否 | 反应计划（如 `停机隔离、标识不良品并100%追溯`；严守零编造，原文未提及时为 `null`） |
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
    "template_version": "v2.0",
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

### 6.3 仅特性清单模式输出包契约 (`CharacteristicListPackage` v2.1)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `characteristic_name` | `string` | 是 | 中文控制计划规范特性名称 |
| `original_name` | `string` | 是 | 特性在原文中的名称（逐字摘录，必须为 `source_quote` 子串） |
| `standard_clauses` | `list[str]` | 否 | 出现该特性的全部标准条款号 |
| `source_quote` | `string` | 是 | 原文引用逐字摘录（防幻觉溯源锚点） |
| `source_page` | `integer \| null` | 否 | 原文所在 PDF 页码（从 1 开始） |
| `is_mandatory` | `boolean` | 是 | 是否为强制要求（建议性/无判据条款为 `false`） |
| `confidence` | `enum` | 是 | 提取置信度枚举：`高` / `中` / `低` |
| `needs_human_review` | `boolean` | 是 | 是否需人工确认（低置信度时强制为 `true`） |
| `notes` | `string \| null` | 否 | 备注说明或门禁拦截提示 |

```json
{
  "standard_info": {
    "file_name": "example_standard.pdf",
    "standard_category": "公差/尺寸标准",
    "template_version": "char_list-v1.0",
    "source_file_url": "/api/source/example_standard.pdf"
  },
  "characteristics": [],
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
    "name_not_in_quote": [],
    "suggestions": []
  }
}
```

---

## 7. 质量门禁（提审结果必须满足）

系统通过 `Pydantic 模型校验器` + `QualityGateValidator` 实施 **6 项确定性质量门禁**、**精准隔离自纠错闭环** 与 **受硬门禁约束的自学习规则引擎**：

1. **条款与原文逐字溯源门禁（硬门禁 - 防幻觉）**：
   - 每条检查项必须包含有效 `standard_clause` 与 `source_quote`；
   - 校验器自动去除空白符比对 `source_quote` 是否真实存在于原文本中，若失配则记入 `missing_clause_or_quote` 并强制分流至 `pending_review_items`。
2. **低置信度 100% 人工确认门禁（软门禁）**：
   - 凡 `confidence == "低"` 的检查项，无论 LLM 原始输出如何，Pydantic 模型层与校验器层双重强制设置 `needs_human_review = true`。
3. **试验方法与判定准则语义防混淆门禁（硬门禁）**：
   - 结合 SQLite 多语言词库（`LexiconDB`），若 `acceptance_criteria` 中出现量检具/试验仪器词条（`method`）且缺乏合格判据特征词（`criteria`），记入 `method_misclassified_as_criteria` 并拦截待审。
4. **数值单位完整性校验**：
   - 对含数值的 `specification` 校验是否具备物理单位、GD&T 符号或标准等级说明，缺失时记入 `unit_inconsistency` 告警。
5. **CTQ 括号参考尺寸拦截门禁**：
   - 自动检测形如 `(120)`、`(Ø62)` 的圆括号参考尺寸（`REF_DIM_PATTERN`），一旦误提则自动拦截至 `pending_review_items` 并提示工程师剔除。
6. **CTQ 单边公差防篡改门禁（硬门禁）**：
   - 当 `source_quote` 含有单边公差（如 `+0.05/-0.00`）而 `specification` 被改写为对称公差 `±` 时，立即判定违反逐字保真原则并拦截报警。
7. **二次语义自纠错闭环 (`refine_items_with_feedback` - 精准打回 + 零丢失对账)**：
   - **精准范围隔离**：仅将触发硬性偏差（条款/原文失配、单边公差篡改、方法与判据混淆）的项送入 `llm_refiner`，单纯因「低置信度/建议性条款」或「参考尺寸」被拦截的项直接保留在 `pending_review_items` 不送修，防止被 LLM 强行编造或误删；
   - **零丢失兜底对账 (`_reconcile_refined_items`)**：对送修项逐一对账，若 LLM 在重提时漏返某项，系统自动回退保留原待审项，严禁静默丢失。
8. **人工复核反馈自学习门禁 (`FeedbackRuleLearner` - 硬门禁绝对优先 + 防误删保护)**：
   - **严格核心名称匹配**：仅允许归一化精确匹配或剥离 `【...工序】` 前缀后的核心特性名精确匹配，禁止宽泛子串模糊包含匹配；
   - **硬门禁绝对优先**：自学习 `approve` 仅允许豁免软门禁（低置信度/参考建议性条款）并自动回填历史学习的规范、检查方法、判定准则及 4 项 IATF 16949 扩展字段（`special_char_class`、`sample_size_frequency`、`control_method`、`reaction_plan`）；若触发原文引用失配、单边公差篡改或方法判据混淆等硬错误，严禁自动放行；
   - **强制条款防误删保护**：自学习 `reject` 仅对原本触发门禁/低置信度的待审或参考项执行自动静默剔除；若新标准文档中该特性带有明确公差且为高置信度强制要求，则自动降级转入 `pending_review_items` 提醒工程师裁定，严防漏检受控 CTQ。

---

## 8. 落地方式与工程实现

1. **文档输入与增强解析**：支持上传 `.pdf`（原生文本层与扫描件 OCR）、`.docx`（自动还原 Word 表格）、`.md`、`.txt`，或直接在 Web 界面按 `Cmd+V` / `Ctrl+V` 粘贴标准截图（调用 macOS Apple Vision / Tesseract OCR）。
2. **长文档语义分块与块级动态路由**：按章节标题与主条款边界智能切分（默认阈值 `3500` 字符），跨块自动携带文档头部与最新 PDF 页码标记 `<!-- 第 X 页 -->`；每个章节分块通过 `classify_chunk_plugins` 动态识别所属类别（无特征词块继承文档主类别，单块多类别高分命中时自动合并注入多插件指令；用户显式指定 `manual_category` 时全局强制覆盖）。
3. **提示词动态组装与在线治理**：按分块类别拼接 `base_template.md` 与对应单/多插件，支持在 Web 工作台实时修改提示词与管理 SQLite 多语言校验词库。
4. **条款页码定位与完整 HTTP 超链接跳转**：自动计算每条检查项对应的 `source_page`，在 Web 界面渲染为可点击胶囊（`📄 条款号 (P.页码) ↗`），并在导出的 15 列 `.xlsx` 报表中自动补全完整 HTTP 基地址（如 `http://127.0.0.1:8000/api/source/...#page=N` 或 `PUBLIC_BASE_URL`），确保本地 Office/WPS 点击即可唤起浏览器精准翻页。
5. **人工确认门禁复核工作台**：工程师可在 Web 端对 `pending_review_items` 在线编辑修正（含规范、检查方法、判定准则及特殊特性分类、抽样频次、控制方法、反应计划）、确认准入（移入 `check_items`）或驳回剔除。
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
| `v1.4` | 2026-11-06 | 归档 | 新增人工复核反馈自学习引擎（`FeedbackRuleLearner`，`src/cp_extractor/core/feedback_learner.py`）：自动观察工程师在待确认项上的手工准入/驳回操作，累计达到可配置阈值 $N$ 次后，自动生成门禁直通/剔除规则、自动回填检查方法与判据、动态注入提示词并扩充 SQLite 量检具词库 |
| `v2.0` | 2026-11-06 | 归档 | 1. **Schema 升级至 15 字段 IATF 16949 完整契约**：`CheckItem` 新增 `special_char_class`（特殊特性分类）、`sample_size_frequency`（样本容量/抽样频次）、`control_method`（控制方法）、`reaction_plan`（反应计划）4 个可选字段，严守“零编造”提取准则并与 `FeedbackRuleLearner` 自学习记忆回填联动；<br>2. **复合标准章节块级动态路由 (`classify_chunk_plugins`)**：支持按章节分块独立识别标准类别与单块多插件融合注入；<br>3. **自学习安全边界加固**：确立“硬门禁绝对优先（原文失配/公差篡改/方法混淆不可绕过）”、“严格核心名称匹配（移除宽泛子串匹配）”与“高置信度强制条款防误删保护”；<br>4. **二次自纠错范围隔离与零丢失兜底 (`_reconcile_refined_items`)**：仅送修硬错误项，保留软门禁待审项，并对送修项逐一对账防漏；<br>5. **Excel 超链接完整 HTTP 基地址补全 (`resolve_full_hyperlink_url`)**：支持本地 Office/WPS 直接点击唤起浏览器跳转 PDF 页码 |
| `v2.1` | 2026-11-10 | 已生效 | 1. **用户认证与鉴权核心**：引入 PBKDF2 加盐散列与 JWT HS256 Token 鉴权，上传与提取接口强制要求登录；内置开箱即用默认管理员 `admin/admin`；<br>2. **全局 API Key 集中管控**：严禁客户端普通用户自行配置大模型 API Key，统一走服务端 `.env`；保护 `/api/llm/save-env` 接口，仅系统管理员允许修改；<br>3. **提示词两级继承架构**：创建 `user_templates` 表，优先加载当前用户定制提示词，未定制回退至系统官方 Markdown 模板，支持单键恢复出厂默认；<br>4. **存储沙箱隔离与防越权 (IDOR) 保护**：用户文件物理隔离于 `output/users/{user_id}/`，严格防护 Path Traversal 与水平越权访问；支持 Header 与 URL `?token=` 双通道下载/预览；<br>5. **自学习规则多租户经验隔离**：`FeedbackRuleLearner` 按 `user_id` 物理隔离学习记忆，不同工程师人工决策互不干扰；<br>6. **Web 工作台 UI 改造**：首页增加登录/注册模态框、Header 用户状态徽章、自动附加 Token 拦截器、管理员专享配置面板与个人定制提示词标识 |

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
