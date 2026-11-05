"""
提示词组装与版本管理模块
负责将公共基础模板与类别插件组合，生成送入 LLM 的完整提示词。
"""

from pathlib import Path
from typing import Optional, Dict, Any, List
import yaml


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
            "3. 每个检查项输出 JSON 字段：\n"
            '   - 特性名称：用控制计划术语命名（若为 GD&T 需注明基准关系，如"位置度 ⌖ (相对于基准 A-B-C)"）\n'
            "   - 特性类别：尺寸/材料/工艺参数/外观/性能\n"
            "   - 规范或公差：逐字保留原文数值、单位、正负号，严禁四舍五入或改写；单边公差（如 `+0.05/-0.00`）必须原样保留，严禁改写为 `±0.05`\n"
            "   - 检查方法：如卡尺、三坐标测量机、拉伸试验、目视、盐雾试验\n"
            "   - 判定准则：合格/不合格的明确条件\n"
            "   - 标准条款：章节号、条款编号或图纸视图区域（如 3.1、SECTION A-A）\n"
            "   - 原文引用：逐字摘录关键原文\n"
            "   - 置信度：高/中/低\n"
            "   - 是否需人工确认：是/否\n"
            '4. 无法确定或字迹模糊无法辨认的内容，严禁猜测或编造，必须将置信度设为"低"且标记"需人工确认"，并在 notes 中说明位置与原因。\n'
            "5. 输出为符合指定 JSON Schema 的 JSON 对象，并附一份可读摘要表。"
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
            'version: "1.1"\n'
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
            "4. 表格中的每一行尺寸分段与公差等级对应关系必须完整保留；跨页表格要合并表头。"
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
            'version: "1.0"\n'
            "---\n"
            "重点关注：化学成分范围、抗拉强度、屈服强度、延伸率、硬度、金相组织、盐雾/老化要求。\n"
            '区分"试验方法"和"合格判据"：试验方法放入检查方法，合格判据放入判定准则。\n'
            '保留单位与试验条件，如"室温、500h盐雾"。'
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
            'version: "1.0"\n'
            "---\n"
            "重点关注：过程参数窗口，如温度区间、保温时间、电流电压范围、焊接速度、涂层厚度。\n"
            '将工艺参数作为"过程特性"检查项，注明适用工序或零件类型。\n'
            '区分"设备要求"和"过程控制参数"，只提取可监控的参数。'
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
            'version: "1.0"\n'
            "---\n"
            "重点关注：缺陷类型清单、试验项目、分级判定标准。\n"
            '定性描述要转化为可判定条件，如"无裂纹、无毛刺、无缩孔"。\n'
            '性能试验提取试验项目与合格判据，如"断后伸长率≥12%"。'
        ),
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

    def build_extraction_prompt(self, category_key: str, context_text: Optional[str] = None) -> str:
        """
        组装提示词：公共基础模板 + 类别专用插件 (+ 待提取文本)
        """
        base = self.get_base_template()
        meta, plugin_body = self.get_plugin(category_key)
        plugin_title = meta.get("name", category_key)

        prompt_parts = [
            base,
            "",
            f"--- 类别专用指导要求（{plugin_title}） ---",
            plugin_body,
        ]

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
