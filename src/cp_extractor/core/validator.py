"""
后处理规则与门禁校验引擎
对提取出的结构化数据执行确定性规则校验、溯源核对与质量门禁。
"""

import re
from typing import List, Optional
from ..schemas.models import (
    CheckItem,
    ConfidenceLevel,
    ExtractionPackage,
    ValidationReport,
)
from .lexicon_db import LexiconDB
from .feedback_learner import FeedbackRuleLearner


class QualityGateValidator:
    """质量门禁与后处理规则校验器（支持从 SQLite 词库实时读取量检具、单位与判据规则）"""

    # 常见试验方法关键词兜底默认值（运行时优先从 SQLite LexiconDB 读取）
    METHOD_KEYWORDS = [
        "试验机", "卡尺", "千分尺", "投影仪", "显微镜", "三坐标", "目视", "盐雾箱",
        "探伤仪", "硬度计", "色差仪", "粗糙度仪", "拉伸试验", "冲击试验", "金相检验"
    ]

    # 常见准则/公差关键词兜底默认值（含 ASME Y14.5 / ISO 1101 14类 GD&T 符号与 CTQ 单位）
    CRITERIA_KEYWORDS = [
        "≥", "≤", ">", "<", "±", "%", "MPa", "mm", "无裂纹", "不得有", "级", "符合",
        "⎯", "⏥", "⌯", "⌭", "⌒", "⌓", "⊥", "∥", "∠", "⌖", "◎", "≡", "↗", "⌕",
        "Ra", "Rz", "HRC", "HV", "HB", "N·m", "Ø", "φ"
    ]

    UNIT_KEYWORDS = [
        "mm", "cm", "m", "%", "MPa", "GPa", "N", "kg", "℃", "°", "h", "s", "min",
        "μm", "nm", "±", "°C", "HRC", "HRB", "HV", "HB", "Ra", "Rz", "N·m", "GU",
        "ppm", "lx", "r/min", "⎯", "⏥", "⌯", "⌭", "⌒", "⌓", "⊥", "∥", "∠", "⌖", "◎", "≡", "↗", "⌕", "Ø", "φ"
    ]

    def __init__(
        self,
        lexicon_db: Optional[LexiconDB] = None,
        feedback_learner: Optional[FeedbackRuleLearner] = None,
    ):
        self.lexicon_db = lexicon_db or LexiconDB()
        self.feedback_learner = feedback_learner or FeedbackRuleLearner(db_path=self.lexicon_db.db_path)

    @staticmethod
    def _contains_term(text: str, term: str) -> bool:
        """
        多语言词条匹配：
        - 对纯英文字母词条（如 CMM、OES、Visual、Grade）使用单词边界大小写不敏感匹配，避免短词误命中其他英文单词内部；
        - 对中/日/德短语及数学公差符号使用大小写不敏感子串匹配。
        """
        if not text or not term:
            return False
        if re.match(r"^[A-Za-z]+$", term):
            return bool(re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE))
        return term.lower() in text.lower()

    # CTQ 规范：匹配带圆括号的参考尺寸，如 (120)、(62)、(Ø62)、（50mm）
    REF_DIM_PATTERN = re.compile(r"^[\(（]\s*[ØφR]?\s*\d+(?:\.\d+)?\s*(?:mm|°|cm)?\s*[\)）]$", re.IGNORECASE)

    # CTQ 规范：匹配单边公差写法，如 +0.05/-0.00、+0.1/0、0/-0.02
    UNILATERAL_TOL_PATTERN = re.compile(r"[+-]?\d+(?:\.\d+)?\s*/\s*[+-]?\d+(?:\.\d+)?")

    # 匹配 PDF 解析时注入的页码标记：<!-- 第 1 页 --> 或 <!-- 第 2 页 (OCR) -->
    PAGE_MARKER_PATTERN = re.compile(r"<!--\s*第\s*(\d+)\s*页[^>]*-->")

    @classmethod
    def resolve_source_page(
        cls,
        source_full_text: str,
        source_quote: str,
        standard_clause: str,
    ) -> Optional[int]:
        """
        根据原文中的 `<!-- 第 X 页 -->` 分页标记，精确定位某条检查项所在的 PDF 页码：
        1. 优先对每个分页块去空白后匹配 `source_quote`；
        2. 若未命中，退而匹配 `standard_clause` 章节号（如 `3.1`）；
        3. 若全文仅有 1 页或无分页标记但确属 PDF，默认返回 None 或對應页码。
        """
        if not source_full_text:
            return None

        markers = list(cls.PAGE_MARKER_PATTERN.finditer(source_full_text))
        if not markers:
            return None

        # 切分出每一页的 (page_num, page_text)
        pages: List[tuple[int, str]] = []
        for i, m in enumerate(markers):
            page_num = int(m.group(1))
            start_pos = m.end()
            end_pos = markers[i + 1].start() if i + 1 < len(markers) else len(source_full_text)
            pages.append((page_num, source_full_text[start_pos:end_pos]))

        clean_quote = re.sub(r"\s+", "", source_quote or "")
        if len(clean_quote) >= 4:
            for page_num, page_txt in pages:
                if clean_quote in re.sub(r"\s+", "", page_txt):
                    return page_num
            # 若完整引用跨行或带表头符号，尝试用前 15 个非空字符匹配
            short_quote = clean_quote[:15]
            if len(short_quote) >= 4:
                for page_num, page_txt in pages:
                    if short_quote in re.sub(r"\s+", "", page_txt):
                        return page_num

        clause = (standard_clause or "").strip()
        if clause and clause not in ("-", "无", "N/A"):
            clause_pattern = re.compile(rf"(?m)(?:^|\s){re.escape(clause)}(?:\b|\s)")
            for page_num, page_txt in pages:
                if clause_pattern.search(page_txt):
                    return page_num

        return pages[0][0] if pages else None

    def validate_package(
        self,
        package: ExtractionPackage,
        source_full_text: Optional[str] = None
    ) -> ExtractionPackage:
        """
        对提取包进行全量规则校验与门禁分流：
        1. 检查条款号与原文引用缺失
        2. 校验低置信度项是否标记人工确认
        3. 检测检查方法与判定准则混淆
        4. 可选：原文引用一致性溯源校验（反幻觉）
        5. 自动分流正常项与待人工确认项 (pending_review_items)
        """
        report = ValidationReport()
        all_candidate_items: List[CheckItem] = package.check_items + [
            i for i in package.pending_review_items if i not in package.check_items
        ]

        valid_items: List[CheckItem] = []
        review_items: List[CheckItem] = []

        # 从 SQLite 词库实时读取最新配置的量检具、物理单位与判定准则关键词
        try:
            grouped = self.lexicon_db.get_grouped_keywords()
        except Exception:
            grouped = {}
        method_keywords = grouped.get("method") or self.METHOD_KEYWORDS
        criteria_keywords = grouped.get("criteria") or self.CRITERIA_KEYWORDS
        unit_keywords = grouped.get("unit") or self.UNIT_KEYWORDS
        sorted_units = sorted((u for u in unit_keywords if u), key=len, reverse=True)
        unit_regex = (
            re.compile("(" + "|".join(re.escape(u) for u in sorted_units) + ")", re.IGNORECASE)
            if sorted_units
            else None
        )

        for idx, item in enumerate(all_candidate_items, start=1):
            item_tag = f"#{idx} [{item.characteristic_name}]"
            needs_review = item.needs_human_review

            # 0. 自动解析并回填 PDF 页码（若原文包含 <!-- 第 X 页 --> 标记）
            if source_full_text:
                resolved_page = self.resolve_source_page(
                    source_full_text, item.source_quote, item.standard_clause
                )
                if resolved_page is not None:
                    item.source_page = resolved_page

            # 1. 溯源校验：标准条款和原文引用
            if not item.standard_clause or item.standard_clause.strip() in ["-", "无", "N/A"]:
                report.missing_clause_or_quote.append(f"{item_tag}: 缺少明确的标准条款号")
                needs_review = True

            if not item.source_quote or len(item.source_quote.strip()) < 2:
                report.missing_clause_or_quote.append(f"{item_tag}: 缺少原文引用内容")
                needs_review = True
            elif source_full_text:
                # 规范要求：每条检查项必须带标准条款和原文引用，抽样审核零编造
                # 清洗换行和空格后核验是否真实存在于原文本
                clean_quote = re.sub(r"\s+", "", item.source_quote)
                clean_source = re.sub(r"\s+", "", source_full_text)
                if clean_quote not in clean_source:
                    report.missing_clause_or_quote.append(
                        f"{item_tag}: 原文引用无法在原文中完全匹配，可能存在改写或幻觉"
                    )
                    needs_review = True

            # 2. 低置信度项门禁
            if item.confidence == ConfidenceLevel.LOW:
                if not item.needs_human_review:
                    report.unmarked_low_confidence.append(
                        f"{item_tag}: 置信度为'低'，已强制标记人工确认"
                    )
                needs_review = True

            # 3. 语义混淆探测：试验方法误写为判定准则（基于 SQLite 多语言词库实时校验）
            for method_kw in method_keywords:
                if self._contains_term(item.acceptance_criteria, method_kw) and not any(
                    self._contains_term(item.acceptance_criteria, c) for c in criteria_keywords
                ):
                    report.method_misclassified_as_criteria.append(
                        f"{item_tag}: 判定准则中包含试验方法关键词 '{method_kw}' 且缺少合格判据特征"
                    )
                    needs_review = True
                    break

            # 4. 单位完整性初筛（数值存在但无单位/判定说明，基于 SQLite 物理单位与多语言判据词库实时校验）
            if re.search(r"\d", item.specification):
                # 含有数字时，检查是否有常见单位、公差符号或多语言等级/符合性判据
                has_unit_or_symbol = (
                    (bool(unit_regex.search(item.specification)) if unit_regex else False)
                    or any(self._contains_term(item.specification, c) for c in criteria_keywords)
                )
                if not has_unit_or_symbol and not re.search(r"(级|級|无|不得|表面|ISO\s*2768|JIS|ASTM|DIN)", item.specification, re.IGNORECASE):
                    report.unit_inconsistency.append(
                        f"{item_tag}: 规范数值 '{item.specification}' 缺少明确物理单位或级别说明"
                    )

            # 5. CTQ 门禁规则 A：括号参考尺寸 (Reference Dimension) 拦截
            if self.REF_DIM_PATTERN.match(item.specification.strip()):
                msg = f"{item_tag}: 检测到括号参考尺寸 '{item.specification}'，按 CTQ 规范参考尺寸非受控，已拦截待确认"
                report.suggestions.append(msg)
                item.notes = item.notes or "括号标注为参考尺寸 (Reference Dimension)，请工程师确认是否剔除"
                needs_review = True

            # 6. CTQ 门禁规则 B：单边公差原样保留校验（防止将 +0.05/-0.00 篡改为 ±0.05）
            if self.UNILATERAL_TOL_PATTERN.search(item.source_quote) and "±" in item.specification and not self.UNILATERAL_TOL_PATTERN.search(item.specification):
                msg = f"{item_tag}: 原文为单边公差，但提取规范被改写为对称公差 '{item.specification}'，违反 CTQ 逐字保真原则"
                report.missing_clause_or_quote.append(msg)
                item.notes = item.notes or "单边公差被改写为双边公差 (±)，请核对原文恢复为单边公差格式"
                needs_review = True

            # 7. 自学习历史人工复核规则 (FeedbackRuleLearner) 自动接管：
            # 若质量工程师已累计多次将该项手工确认准入 (approve) 或驳回 (reject) 达到设定阈值，自动执行放行或剔除
            matched_rule = None
            try:
                matched_rule = self.feedback_learner.match_active_rule(
                    package.standard_info.standard_category,
                    item.characteristic_name,
                )
            except Exception:
                matched_rule = None

            if matched_rule:
                if matched_rule["action"] == "reject":
                    report.suggestions.append(
                        f"🧠 自学习规则已自动剔除「{item.characteristic_name}」（历史人工累计驳回 {matched_rule['reject_count']} 次）。"
                    )
                    continue
                elif matched_rule["action"] == "approve":
                    if matched_rule["learned_specification"] and (
                        "待" in item.specification or "未明确" in item.specification or "仅供参考" in item.specification
                    ):
                        item.specification = matched_rule["learned_specification"]
                    if matched_rule["learned_method"] and (
                        item.inspection_method in ("待确认", "待定", "") or "待" in item.inspection_method
                    ):
                        item.inspection_method = matched_rule["learned_method"]
                    if matched_rule["learned_criteria"] and (
                        item.acceptance_criteria in ("待确认", "待定", "") or "待" in item.acceptance_criteria
                    ):
                        item.acceptance_criteria = matched_rule["learned_criteria"]

                    item.confidence = ConfidenceLevel.HIGH
                    needs_review = False
                    item.notes = f"🧠 自学习规则已自动准入（历史人工累计确认 {matched_rule['approve_count']} 次）"
                    # 移除因原始低置信度记录的告警
                    report.unmarked_low_confidence = [
                        m for m in report.unmarked_low_confidence if not m.startswith(item_tag)
                    ]
                    report.suggestions.append(
                        f"🧠 自学习规则已将「{item.characteristic_name}」自动准入控制计划（历史人工确认 {matched_rule['approve_count']} 次）。"
                    )

            # 更新 items
            item.needs_human_review = needs_review
            if needs_review:
                review_items.append(item)
            else:
                valid_items.append(item)

        # 汇总建议
        if review_items:
            report.suggestions.append(f"共有 {len(review_items)} 条检查项触发门禁，需质量工程师人工复核。")
        if report.missing_clause_or_quote:
            report.suggestions.append("检测到部分条款/原文缺失或未匹配，建议抽检对应原始标准章节。")

        package.check_items = valid_items
        package.pending_review_items = review_items
        package.validation_report = report
        package.refresh_summary()

        return package
