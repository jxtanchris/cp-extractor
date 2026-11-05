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


class QualityGateValidator:
    """质量门禁与后处理规则校验器"""

    # 常见试验方法关键词（用于检测是否误填入判定准则）
    METHOD_KEYWORDS = [
        "试验机", "卡尺", "千分尺", "投影仪", "显微镜", "三坐标", "目视", "盐雾箱",
        "探伤仪", "硬度计", "色差仪", "粗糙度仪", "拉伸试验", "冲击试验", "金相检验"
    ]

    # 常见准则/公差关键词（用于检测是否误填入检查方法，含 ASME Y14.5 / ISO 1101 14类 GD&T 符号与 CTQ 单位）
    CRITERIA_KEYWORDS = [
        "≥", "≤", ">", "<", "±", "%", "MPa", "mm", "无裂纹", "不得有", "级", "符合",
        "⎯", "⏥", "⌯", "⌭", "⌒", "⌓", "⊥", "∥", "∠", "⌖", "◎", "≡", "↗", "⌕",
        "Ra", "Rz", "HRC", "HV", "HB", "N·m", "Ø", "φ"
    ]

    # CTQ 规范：匹配带圆括号的参考尺寸，如 (120)、(62)、(Ø62)、（50mm）
    REF_DIM_PATTERN = re.compile(r"^[\(（]\s*[ØφR]?\s*\d+(?:\.\d+)?\s*(?:mm|°|cm)?\s*[\)）]$", re.IGNORECASE)

    # CTQ 规范：匹配单边公差写法，如 +0.05/-0.00、+0.1/0、0/-0.02
    UNILATERAL_TOL_PATTERN = re.compile(r"[+-]?\d+(?:\.\d+)?\s*/\s*[+-]?\d+(?:\.\d+)?")

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

        for idx, item in enumerate(all_candidate_items, start=1):
            item_tag = f"#{idx} [{item.characteristic_name}]"
            needs_review = item.needs_human_review

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

            # 3. 语义混淆探测：试验方法误写为判定准则
            for method_kw in self.METHOD_KEYWORDS:
                if method_kw in item.acceptance_criteria and not any(c in item.acceptance_criteria for c in self.CRITERIA_KEYWORDS):
                    report.method_misclassified_as_criteria.append(
                        f"{item_tag}: 判定准则中包含试验方法关键词 '{method_kw}' 且缺少合格判据特征"
                    )
                    needs_review = True
                    break

            # 4. 单位完整性初筛（数值存在但无单位/判定说明）
            if re.search(r"\d", item.specification):
                # 含有数字时，检查是否有常见单位或公差符号
                has_unit_or_symbol = bool(re.search(r"(mm|cm|m|%|MPa|GPa|N|kg|℃|°|h|s|min|μm|nm|±|°C|HRC|HV|HB|Ra|Rz|⎯|⏥|⌯|⌭|⌒|⌓|⊥|∥|∠|⌖|◎|≡|↗|⌕)", item.specification, re.IGNORECASE))
                if not has_unit_or_symbol and not re.search(r"(级|无|不得|表面|ISO\s*2768)", item.specification, re.IGNORECASE):
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
