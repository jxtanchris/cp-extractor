"""
提示词组装与版本管理模块
负责将公共基础模板与类别插件组合，生成送入 LLM 的完整提示词。
"""

from pathlib import Path
from typing import Optional, Dict
import yaml


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
