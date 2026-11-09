"""
提示词组装与版本管理模块
负责将公共基础模板与类别插件组合，生成送入 LLM 的完整提示词。
"""

from pathlib import Path
from typing import Optional, Dict, Any, List, Union
import yaml

from .feedback_learner import FeedbackRuleLearner


# 仅特性清单模式（char_list）出厂默认模板：只提取特性名称，不提取规范/方法/判据
CHAR_LIST_DEFAULT_CONTENT = """你是汽车行业质量工程师（IATF 16949）。任务：通读下面的标准原文，**列出其中所有可检查、可验证的产品特性与过程特性的名称**。
本任务只关心「有哪些特性」，**不需要**提取公差数值、检查方法或判定准则。

## 扫描范围（宁可多列，不可漏列）
逐条扫描正文、表格、表注、脚注与附录，对照以下清单：
- 尺寸/公差类：线性/角度尺寸、GD&T 形位公差（注明基准）、表面粗糙度、螺纹、倒角/圆角、未注公差
- 材料类：化学成分（**每个元素单独一条**，如 C、Si、Mn、P、S）、抗拉强度、屈服强度、延伸率、硬度、冷弯、金相组织、晶粒度
- 工艺类：热处理温度/时间/碳势、焊接参数、涂装参数、拧紧扭矩等（名称前加【适用工序】，如【强渗工序】渗碳温度）
- 外观/性能类：各类表面缺陷、色差、光泽、膜厚、附着力、耐盐雾/耐候、耐冲击、耐化学品等

## 纳入规则
1. 带「应、必须、不小于、不大于、符合……」等强制要求的特性 → `is_mandatory: true`
2. 带「宜、参考、协商、不作为判据」的特性，或仅提到特性但未给出判据的 → **同样列出**，`is_mandatory: false`
3. 不列入：圆括号参考尺寸（如 (120)）、图号/标题栏/修订记录、设备定期校验与维护要求

## 粒度与去重
- 一个被测量 = 一条。同一特性的多个尺寸分段/等级行合并为一条，不要按表格行拆分。
- 同一特性出现在多个条款时只输出一条，`standard_clauses` 列出全部条款号。

## 字段说明（先引原文，再给名称）
- `standard_clauses`：条款号数组，如 ["3.1", "表2"]
- `source_quote`：逐字摘录包含该特性的原文短句（不得改写，80 字以内）
- `original_name`：特性在原文中的名称，逐字复制，必须是 `source_quote` 的子串
- `characteristic_name`：中文控制计划规范名（原文为外文时翻译；GD&T 注明基准）
- `is_mandatory`：true / false
- `confidence`：高/中/低；无法辨认或不确定时为「低」并在 `notes` 说明原因
- `notes`：可选"""


DEFAULT_TEMPLATES: Dict[str, Dict[str, str]] = {
    "base": {
        "title": "🌐 公共基础模板 (全局判定规则)",
        "rel_path": "base_template.md",
        "description": "控制所有标准通用的提取范围、Product/Process CTQ 分类、置信度评估及「是否需人工确认」的全局判定准则。",
        "default_content": (
            '你是汽车行业制造质量工程师（IATF 16949 / CTQ 专家）。请系统性分析技术标准或工程图纸文本，提取所有"可检查、可验证"的关键质量特性（Critical-To-Quality, CTQ），用于生成控制计划检查项。\n\n'
            "输出要求：\n"
            "1. 系统性扫描并分类提取两类核心特性：\n"
            "   - 产品关键特性 (Product CTQ)：线性/角度尺寸、双边/单边公差、GD&T 形位公差控制框、表面粗糙度 (Ra/Rz)、螺纹规格、倒角/圆角、材料理化及外观性能。\n"
            "   - 过程关键特性 (Process CTQ)：特殊工艺窗口、热处理参数、硬度要求 (如 HRC 58-62)、涂装/镀层厚度、焊接规范、拧紧扭矩规范等。\n"
            '2. 只提取明确要求"应、必须、不小于、不大于、符合……级"或带明确公差带的受控要求；严格排除非受控信息：\n'
            "   - 【排除参考尺寸】严禁提取带圆括号的参考/辅助尺寸，如 `(120)`、`(62)`、`(Ø62)`。\n"
            "   - 【排除非受控文本】跳过图号、标题栏、修订记录及无合格/不合格判据的说明文字。\n"
            "3. 每个检查项输出 JSON 字段（严格遵循【先引原文锚定 -> 再提取参数】顺序，确保零幻觉）：\n"
            "   - 标准条款 (`standard_clause`)：章节号、条款编号或图纸视图区域（如 3.1、SECTION A-A）\n"
            "   - 原文引用 (`source_quote`)：逐字摘录原文关键句（严禁改写或缩略数值），作为后续字段的唯一事实依据\n"
            '   - 特性名称 (`characteristic_name`)：用控制计划术语命名（若为 GD&T 需注明基准关系，如"位置度 ⌖ (相对于基准 A-B-C)"）\n'
            "   - 特性类别 (`characteristic_category`)：尺寸/材料/工艺参数/外观/性能\n"
            "   - 特殊特性分类 (`special_char_class`)：如 CC/SC/PTC/关键特性/重要特性，仅在原文明确标注时提取，否则填 null\n"
            "   - 规范或公差 (`specification`)：逐字保留原文数值、单位、正负号，严禁四舍五入或改写；单边公差（如 `+0.05/-0.00`）必须原样保留，严禁改写为 `±0.05`\n"
            "   - 检查方法 (`inspection_method`)：只填量检具、仪器或试验标准名称（如卡尺、三坐标测量机、按 GB/T 228.1 拉伸试验）\n"
            "   - 判定准则 (`acceptance_criteria`)：只填合格/不合格的明确数值边界或状态条件，严禁把量检具名称填入此字段\n"
            "   - 样本容量与抽样频次 (`sample_size_frequency`)：如 100%、5件/每班、首末件抽检，仅在原文明确提及时提取，否则填 null（严禁编造）\n"
            "   - 控制方法 (`control_method`)：如 SPC控制图、首末件记录单、防错点检表、在线连续监控记录，仅在原文明确提及时提取，否则填 null\n"
            "   - 反应计划 (`reaction_plan`)：如 停机隔离、标识不良品并100%追溯，仅在原文明确提及时提取，否则填 null（严禁编造）\n"
            "   - 置信度 (`confidence`)：高/中/低\n"
            "   - 是否需人工确认 (`needs_human_review`)：是/否\n"
            '4. 无法确定或字迹模糊无法辨认的内容，严禁猜测或编造，必须将置信度设为"低"且标记"需人工确认"，并在 notes 中说明位置与原因。\n'
            "5. 输出为符合指定 JSON Schema 的 JSON 对象，并附一份可读摘要表。\n"
            '- 【建议性条款拦截规则】对于带有"宜、参考、由供需双方协商、不做交货否决判据"的条款，仍需提取为检查项，但必须将 confidence 设为"低"且 needs_human_review 设为 true，交由质量工程师裁定是否纳入控制计划。\n'
            "- 【CTQ & GD&T 专项规则】① 严禁提取带圆括号的参考尺寸（如 (120)、(Ø62)）；② 单边公差（如 +0.05/-0.00）必须 100% 逐字保留，严禁折算改写为 ±0.05；③ 提取 GD&T 形位公差（⎯, ⏥, ⌯, ⌭, ⌒, ⌓, ⊥, ∥, ∠, ⌖, ◎, ≡, ↗, ⌕）时需注明关联基准（如 A-B-C）并完整保留控制框描述。"
        ),
    },
    "tolerance": {
        "title": "📐 公差/尺寸标准插件",
        "rel_path": "plugins/tolerance.md",
        "description": "针对线性尺寸公差、14类 GD&T 形位公差控制框、单边公差保真、括号参考尺寸过滤及未注公差表的专用提取指令。",
        "default_content": (
            "---\n"
            "category: tolerance\n"
            "name: 公差/尺寸标准插件 (含 CTQ & GD&T 规范)\n"
            'version: "1.2"\n'
            "---\n"
            "重点关注：尺寸公差、GD&T 形位公差特征控制框、基准体系、最大/最小实体要求 (MMC/LMC)、表面粗糙度 (Ra/Rz)、螺纹/圆角/倒角、未注公差表 (如 ISO 2768)。\n"
            "1. 【单边公差原样保留 (Verbatim Tolerances)】单边公差如 `+0.05/-0.00` 或 `0/-0.02` 必须 100% 逐字保留，严禁简化或折算为双边公差 `±0.05`。\n"
            "2. 【过滤括号参考尺寸】严禁提取带圆括号的参考尺寸（如 `(120)`、`(62)`、`(φ62)`），此类尺寸仅作参考，不纳入控制计划检验。\n"
            "3. 【GD&T 14 类几何特征控制框解析 (ASME Y14.5 / ISO 1101)】：\n"
            "   - 形状公差：直线度 (⎯)、平面度 (⏥)、圆度 (⌯)、圆柱度 (⌭)\n"
            "   - 轮廓公差：线轮廓度 (⌒)、面轮廓度 (⌓)\n"
            "   - 方向公差：垂直度 (⊥)、平行度 (∥)、倾斜度 (∠)\n"
            "   - 位置与跳动：位置度 (⌖)、同轴/同心度 (◎)、对称度 (≡)、圆跳动 (↗)、全跳动 (⌕)\n"
            '   - GD&T 符号必须保留原符号或标准缩写，特性名称需包含几何特征与基准（如"位置度 ⌖ (基准 A-B-C)"），规范字段完整保留控制框表达式（如 `⌖ Ø0.05 (MMC) A B C`）。\n'
            "4. 表格中的每一行尺寸分段与公差等级对应关系必须逐行独立提取，严禁将多行尺寸分段合并为一条；跨页表格要合并表头。\n\n"
            "【Few-Shot 黄金正反例参考】\n"
            "- ✅ 正例（单边公差保真）：原文“3.1 主定位孔孔径 D1 必须控制在 Ø16.00 +0.05/-0.00 mm，采用塞规检验” -> `specification`: `\"Ø16.00 +0.05/-0.00 mm\"`（严禁改写为 `±0.05`），`inspection_method`: `\"塞规\"`，`acceptance_criteria`: `\"Ø16.00 ~ Ø16.05 mm（通规通，止规止）\"`。\n"
            "- ❌ 反例（括号参考尺寸过滤）：原文“3.3 支架参考总长 (385 mm)” -> 带圆括号属于非受控参考尺寸，直接忽略不提取！"
        ),
    },
    "material": {
        "title": "🧪 材料标准插件",
        "rel_path": "plugins/material.md",
        "description": "针对化学成分、力学性能、金相组织等材料标准的专用提取与确认指令。",
        "default_content": (
            "---\n"
            "category: material\n"
            "name: 材料标准插件\n"
            'version: "1.1"\n'
            "---\n"
            "重点关注：化学成分范围、抗拉强度、屈服强度、延伸率、硬度、金相组织、盐雾/老化要求。\n"
            "1. 化学成分表中的每一个元素（如 C、Si、Mn、P、S、Alt）必须逐项独立拆分为单独的检查项，不得合并为笼统的一条“化学成分”。\n"
            '2. 严格区分"试验方法"和"合格判据"：试验仪器与标准号（如“按 GB/T 228.1 电子万能试验机拉伸”）放入 `inspection_method`，合格数值范围（如 `ReH ≥ 420 MPa`）放入 `acceptance_criteria`。\n'
            '3. 保留单位与试验条件，如"室温 23±5℃、500h盐雾"。\n\n'
            "【Few-Shot 黄金正反例参考】\n"
            "- ✅ 正例（方法与判据分离）：原文“3.1 按 GB/T 228.1 进行室温拉伸试验，屈服强度 ReH 应不小于 420 MPa” -> `specification`: `\"ReH ≥ 420 MPa\"`，`inspection_method`: `\"室温拉伸试验（按 GB/T 228.1）\"`，`acceptance_criteria`: `\"ReH ≥ 420 MPa\"`。\n"
            "- ❌ 反例（参考项门禁拦截）：原文“3.3 表面洛氏硬度供内部参考，约 75~88 HRB，不做交货否决判据” -> 提取该项但必须设置 `confidence`: `\"低\"`, `needs_human_review`: `true`。"
        ),
    },
    "process": {
        "title": "🔥 工艺标准插件",
        "rel_path": "plugins/process.md",
        "description": "针对热处理、焊接、涂装等过程参数窗口的专用提取与确认指令。",
        "default_content": (
            "---\n"
            "category: process\n"
            "name: 工艺标准插件\n"
            'version: "1.1"\n'
            "---\n"
            "重点关注：过程参数窗口，如温度区间、保温时间、碳势、电流电压范围、焊接速度、淬火油温与搅拌转速。\n"
            '1. 将工艺参数作为"过程特性"（`工艺参数`）检查项，在特性名称前注明适用工序（如 `【强渗工序】渗碳温度`）。\n'
            '2. 严格区分"设备管理/工装要求"和"过程控制参数"：只提取生产过程中针对产品批次在线监控的工艺参数；设备定期校验、空炉测温、备件配置等条款一律跳过不提取。\n\n'
            "【Few-Shot 黄金正反例参考】\n"
            "- ✅ 正例（过程控制参数）：原文“3.1 强渗阶段渗碳温度必须控制在 920 ± 10 ℃，由炉温系统在线监控” -> 提取为 `【强渗工序】渗碳温度`，`specification`: `\"920 ± 10 ℃\"`。\n"
            "- ❌ 反例（设备要求剔除）：原文“2.1 多用炉有效加热区应每季度按 AMS 2750 进行一次九点炉温均匀性测试（±5℃）” -> 属于设备定期维护校验要求而非产品生产过程控制参数，直接跳过不提取！"
        ),
    },
    "appearance_performance": {
        "title": "✨ 外观/性能标准插件",
        "rel_path": "plugins/appearance_performance.md",
        "description": "针对表面缺陷限度、盐雾耐候、附着力等外观及性能标准的专用提取与确认指令。",
        "default_content": (
            "---\n"
            "category: appearance_performance\n"
            "name: 外观/性能标准插件\n"
            'version: "1.1"\n'
            "---\n"
            "重点关注：缺陷类型清单、目视光源与视距条件、色差光泽度、涂层厚度、附着力、耐盐雾/耐候性能。\n"
            '1. 定性外观描述要转化为可量化的零缺陷判定条件（如 `0 流挂、0 露底、0 气泡`），并保留观测条件（如 `D65 光源 ≥1000 lx，视距 500mm`）。\n'
            '2. 严格区分 `外观`（目视缺陷、色差、光泽）与 `性能`（膜厚、附着力、耐盐雾腐蚀、耐冲击）。\n\n'
            "【Few-Shot 黄金正反例参考】\n"
            "- ✅ 正例（定性转定量 + 试验条件保留）：原文“3.3 按 GB/T 10125 连续喷雾 720h 后，涂层表面无起泡、无生锈，划痕单侧扩蚀宽度 ≤ 2.0 mm（卡尺测量）” -> `characteristic_category`: `\"性能\"`，`inspection_method`: `\"中性盐雾试验 720h（按 GB/T 10125）+ 目视及游标卡尺测量\"`，`acceptance_criteria`: `\"无起泡、无生锈，且划痕单侧扩蚀宽度 ≤ 2.0 mm\"`。"
        ),
    },
    "char_list": {
        "title": "📋 仅特性清单模板 (只提取特性名称)",
        "rel_path": "characteristic_list_template.md",
        "description": "仅特性清单模式专用：偏召回地列出标准中所有特性名称（中文规范名 + 原文名），不提取规范/公差、检查方法与判定准则；不叠加类别插件。",
        "default_content": CHAR_LIST_DEFAULT_CONTENT,
    },
}


class PromptBuilder:
    def __init__(self, templates_dir: Optional[Path] = None):
        if templates_dir is None:
            # 默认为项目根目录下的 templates 目录
            self.templates_dir = Path(__file__).resolve().parent.parent.parent.parent / "templates"
        else:
            self.templates_dir = Path(templates_dir)

        self.plugins_dir = self.templates_dir / "plugins"

    def _load_template_file(self, path: Path) -> str:
        if not path.exists():
            raise FileNotFoundError(f"模板文件不存在: {path}")
        return path.read_text(encoding="utf-8").strip()

    def _parse_frontmatter(self, text: str) -> tuple[Dict, str]:
        """解析 markdown 前置元数据 (Frontmatter)"""
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1]) or {}
                    body = parts[2].strip()
                    return meta, body
                except yaml.YAMLError:
                    pass
        return {}, text

    def get_base_template(self) -> str:
        base_path = self.templates_dir / "base_template.md"
        return self._load_template_file(base_path)

    def get_plugin(self, category_key: str) -> tuple[Dict, str]:
        """
        加载指定类别的插件指令。
        category_key 可以是: tolerance, material, process, appearance_performance
        """
        plugin_file = self.plugins_dir / f"{category_key}.md"
        if not plugin_file.exists():
            raise ValueError(f"未找到对应的类别插件: {category_key} (路径: {plugin_file})")
        content = self._load_template_file(plugin_file)
        return self._parse_frontmatter(content)

    def get_validation_template(self) -> str:
        val_path = self.templates_dir / "validation_template.md"
        content = self._load_template_file(val_path)
        _, body = self._parse_frontmatter(content)
        return body

    def build_extraction_prompt(
        self,
        category_key: Union[str, List[str]],
        context_text: Optional[str] = None,
    ) -> str:
        """
        组装提示词：公共基础模板 + 类别专用插件(支持单插件或多插件联合注入) (+ 自学习历史复核规则) (+ 待提取文本)
        """
        base = self.get_base_template()
        plugin_keys = [category_key] if isinstance(category_key, str) else list(category_key)
        if not plugin_keys:
            plugin_keys = ["tolerance"]

        prompt_parts = [base]
        for pk in plugin_keys:
            meta, plugin_body = self.get_plugin(pk)
            plugin_title = meta.get("name", pk)
            prompt_parts.extend([
                "",
                f"--- 类别专用指导要求（{plugin_title}） ---",
                plugin_body,
            ])
            try:
                learned_block = FeedbackRuleLearner().build_prompt_injection_block(pk)
                if learned_block:
                    prompt_parts.extend(["", learned_block])
            except Exception:
                pass

        if context_text:
            prompt_parts.extend([
                "",
                "--- 待提取的标准原文内容 ---",
                context_text,
            ])

        return "\n".join(prompt_parts)

    def get_characteristic_list_template(self) -> str:
        """加载仅特性清单模式模板（文件缺失时回退出厂默认内容）"""
        path = self.templates_dir / DEFAULT_TEMPLATES["char_list"]["rel_path"]
        if not path.exists():
            return CHAR_LIST_DEFAULT_CONTENT.strip()
        _, body = self._parse_frontmatter(self._load_template_file(path))
        return body

    def build_characteristic_list_prompt(self, context_text: Optional[str] = None) -> str:
        """
        组装仅特性清单模式提示词：单一精简模板 (+ 待提取文本)。
        不叠加类别插件，也不注入自学习规则（自学习规则面向规范/方法/判据回填，与本模式无关）。
        """
        prompt_parts = [self.get_characteristic_list_template()]
        if context_text:
            prompt_parts.extend([
                "",
                "--- 待提取的标准原文内容 ---",
                context_text,
            ])
        return "\n".join(prompt_parts)

    def _resolve_template_path(self, key: str) -> Path:
        if key not in DEFAULT_TEMPLATES:
            raise ValueError(f"不支持的模板标识: {key}")
        return self.templates_dir / DEFAULT_TEMPLATES[key]["rel_path"]

    def list_all_templates(self) -> List[Dict[str, Any]]:
        """返回所有可编辑模板的当前内容与元信息"""
        items: List[Dict[str, Any]] = []
        for key, info in DEFAULT_TEMPLATES.items():
            fpath = self._resolve_template_path(key)
            current = fpath.read_text(encoding="utf-8").strip() if fpath.exists() else info["default_content"]
            items.append({
                "key": key,
                "title": info["title"],
                "rel_path": f"templates/{info['rel_path']}",
                "description": info["description"],
                "content": current,
                "default_content": info["default_content"],
                "is_modified": current != info["default_content"].strip(),
            })
        return items

    def save_template(self, key: str, content: str) -> Dict[str, Any]:
        """保存用户编辑后的模板内容到磁盘"""
        cleaned = content.strip()
        if not cleaned:
            raise ValueError("模板内容不能为空")
        fpath = self._resolve_template_path(key)
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(cleaned + "\n", encoding="utf-8")
        info = DEFAULT_TEMPLATES[key]
        return {
            "key": key,
            "title": info["title"],
            "rel_path": f"templates/{info['rel_path']}",
            "content": cleaned,
            "is_modified": cleaned != info["default_content"].strip(),
        }

    def reset_template(self, key: str) -> Dict[str, Any]:
        """将指定模板恢复为系统出厂默认内容"""
        if key not in DEFAULT_TEMPLATES:
            raise ValueError(f"不支持的模板标识: {key}")
        default_text = DEFAULT_TEMPLATES[key]["default_content"]
        return self.save_template(key, default_text)
