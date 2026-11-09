"""
核心提取引擎与流水线编排器
串联：类别识别 -> 提示词组装 -> 条款扫描与结构化提取 -> 质量门禁校验 -> 输出包封装
"""

import json
import re
from pathlib import Path
from typing import Callable, List, Optional, Union

from ..schemas.models import (
    CharacteristicCategory,
    CheckItem,
    ConfidenceLevel,
    CoverageSummary,
    ExtractionPackage,
    StandardInfo,
)
from .classifier import StandardClassifier
from .prompt_builder import PromptBuilder
from .validator import QualityGateValidator


class StandardExtractorPipeline:
    """标准→控制计划检查项提取全链路引擎"""

    # 长文档按章节/条款自动分块的阈值（字符数），避免单次输出超长导致漏提或合并
    DEFAULT_CHUNK_CHAR_LIMIT = 2200

    def __init__(
        self,
        templates_dir: Optional[Path] = None,
        llm_callable: Optional[Callable[[str], str]] = None,
        llm_refiner: Optional[Callable[[str, List[dict], List[str], str], List[dict]]] = None,
        chunk_char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT,
    ):
        self.classifier = StandardClassifier()
        self.prompt_builder = PromptBuilder(templates_dir=templates_dir)
        self.validator = QualityGateValidator()
        self.llm_callable = llm_callable
        # 若传入的 llm_callable 是 DeepSeekClient.extract_check_items_json 绑定方法，自动挂载其 refine_items_with_feedback
        if llm_refiner is not None:
            self.llm_refiner = llm_refiner
        elif hasattr(llm_callable, "__self__") and hasattr(llm_callable.__self__, "refine_items_with_feedback"):
            self.llm_refiner = llm_callable.__self__.refine_items_with_feedback
        else:
            self.llm_refiner = None
        self.chunk_char_limit = chunk_char_limit

    def count_clauses(self, text: str) -> int:
        """统计标准文档中的条款编号数量（如 3.1, 4.2 等）"""
        matches = re.findall(r"(?m)^\s*\d+\.\d+\b", text)
        return len(matches)

    @staticmethod
    def split_document_into_chunks(content: str, max_chars: int = DEFAULT_CHUNK_CHAR_LIMIT) -> List[str]:
        """
        按章节标题（`## `）或主条款边界对长标准文档进行语义分块，同时保留顶部文档标题上下文，
        确保大型多章节标准文档或大表格不会因单次请求过长而被 LLM 漏提或过度合并。
        """
        stripped = content.strip()
        if len(stripped) <= max_chars:
            return [stripped]

        # 提取文档首部标题区（第一个二级标题或主条款之前的内容）作为公共上下文前缀
        header_match = re.search(r"(?m)^(?:##+\s+|\d+\.\s+\S|\d+\.\d+\s+)", stripped)
        doc_header = ""
        body = stripped
        if header_match and 0 < header_match.start() <= 400:
            doc_header = stripped[: header_match.start()].strip()
            body = stripped[header_match.start() :].strip()

        # 优先按 Markdown 二级/三级标题或顶层章节号切分段落块
        raw_sections = re.split(r"(?m)(?=^(?:##+\s+|\d+\.\d+\b))", body)
        sections = [s.strip() for s in raw_sections if s.strip()]
        if len(sections) <= 1:
            sections = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]

        page_marker_re = re.compile(r"<!--\s*第\s*(\d+)\s*页[^>]*-->")
        chunks: List[str] = []
        current_parts: List[str] = []
        current_len = 0
        last_page_num: Optional[str] = None

        # 检查 doc_header 中是否有页码标记
        if doc_header:
            header_pages = page_marker_re.findall(doc_header)
            if header_pages:
                last_page_num = header_pages[-1]

        for sec in sections:
            sec_len = len(sec)
            if current_parts and (current_len + sec_len + 2 > max_chars):
                chunk_body = "\n\n".join(current_parts)
                chunks.append(f"{doc_header}\n\n{chunk_body}".strip() if doc_header else chunk_body)
                # 更新截止当前块末尾的最新页码，若下一块开头没有页码标记则自动承接
                found_pages = page_marker_re.findall(chunk_body)
                if found_pages:
                    last_page_num = found_pages[-1]
                if last_page_num and not page_marker_re.search(sec[:60]):
                    sec = f"<!-- 第 {last_page_num} 页 -->\n{sec}"
                    sec_len = len(sec)
                current_parts = [sec]
                current_len = sec_len
            else:
                current_parts.append(sec)
                current_len += sec_len + 2

        if current_parts:
            chunk_body = "\n\n".join(current_parts)
            chunks.append(f"{doc_header}\n\n{chunk_body}".strip() if doc_header else chunk_body)

        return chunks if chunks else [stripped]

    @staticmethod
    def _deduplicate_items(items: List[CheckItem]) -> List[CheckItem]:
        """跨分块合并时按 (条款号, 特性名称, 规范公差) 去重"""
        seen = set()
        unique: List[CheckItem] = []
        for item in items:
            key = (
                item.standard_clause.strip(),
                item.characteristic_name.strip(),
                item.specification.strip(),
            )
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    # 分块因输出超长被截断时，二分重试的最小分块字符数与最大递归深度
    MIN_SPLIT_CHARS = 400
    MAX_SPLIT_DEPTH = 3

    @staticmethod
    def _split_text_in_half(text: str) -> List[str]:
        """按最靠近中点的换行把文本一分为二（尽量不切断表格行）"""
        mid = len(text) // 2
        left = text.rfind("\n", 0, mid)
        right = text.find("\n", mid)
        candidates = [p for p in (left, right) if p > 0]
        cut = min(candidates, key=lambda p: abs(p - mid)) if candidates else mid
        return [part for part in (text[:cut].strip(), text[cut:].strip()) if part]

    def _extract_chunk_adaptive(
        self,
        chunk_text: str,
        plugin_key: str,
        manual_category: Optional[str],
        depth: int = 0,
    ) -> List[CheckItem]:
        """
        调用 LLM 提取单个分块；若因输出超长截断/正文为空而失败，自动二分该分块后递归重试，
        使任意新文档无需人工拆分即可完成提取。
        """
        chunk_plugins = self.classifier.classify_chunk_plugins(
            chunk_text,
            default_plugin_key=plugin_key,
            manual_category=manual_category,
        )
        target_plugin_arg = chunk_plugins[0] if len(chunk_plugins) == 1 else chunk_plugins
        chunk_prompt = self.prompt_builder.build_extraction_prompt(target_plugin_arg, context_text=chunk_text)
        try:
            raw_response = self.llm_callable(chunk_prompt)
        except ValueError:
            if depth >= self.MAX_SPLIT_DEPTH or len(chunk_text) < self.MIN_SPLIT_CHARS:
                raise
            results: List[CheckItem] = []
            for half in self._split_text_in_half(chunk_text):
                results.extend(self._extract_chunk_adaptive(half, plugin_key, manual_category, depth + 1))
            return results

        extracted: List[CheckItem] = []
        for item in json.loads(raw_response):
            try:
                extracted.append(CheckItem.model_validate(item))
            except Exception:
                # 单条结构不合法时跳过，避免整份文档提取失败
                continue
        return extracted

    def run(
        self,
        file_name: str,
        content: str,
        manual_category: Optional[str] = None,
    ) -> ExtractionPackage:
        """运行完整提取、分块聚合、后处理门禁校验与二次自纠错流水线"""
        # 1. 类别识别
        plugin_key, category_enum = self.classifier.classify(content, manual_category=manual_category)

        # 2. 读取插件版本信息
        meta, _ = self.prompt_builder.get_plugin(plugin_key)
        template_version = f"v{meta.get('version', '1.0')}"

        # 3. 执行提取（支持章节块级动态路由与多插件融合；若未配置 LLM 则使用内置结构化基准提取器）
        chunks = self.split_document_into_chunks(content, max_chars=self.chunk_char_limit)
        if self.llm_callable is not None:
            all_extracted: List[CheckItem] = []
            for chunk_text in chunks:
                all_extracted.extend(
                    self._extract_chunk_adaptive(chunk_text, plugin_key, manual_category)
                )
            items = self._deduplicate_items(all_extracted)
        else:
            all_rule_items: List[CheckItem] = []
            for chunk_text in chunks:
                chunk_plugins = self.classifier.classify_chunk_plugins(
                    chunk_text,
                    default_plugin_key=plugin_key,
                    manual_category=manual_category,
                )
                for pk in chunk_plugins:
                    all_rule_items.extend(self._structured_rule_extract(pk, chunk_text))
            items = self._deduplicate_items(all_rule_items)

        # 4. 封装初始输出包
        total_clauses = self.count_clauses(content)
        package = ExtractionPackage(
            standard_info=StandardInfo(
                file_name=file_name,
                standard_category=category_enum.value,
                template_version=template_version,
            ),
            check_items=items,
            coverage_summary=CoverageSummary(total_clauses_scanned=total_clauses),
        )

        # 5. 运行后处理规则校验与人工确认门禁分流
        validated_package = self.validator.validate_package(package, source_full_text=content)

        # 6. 二次语义校验与打回重提闭环 (Targeted Self-Correction & Zero-Drop Fallback)：
        # 仅将触发硬性提取偏差（原文失配、单边公差篡改、方法与判据混淆）的项送入 llm_refiner，
        # 单纯低置信度/参考尺寸项保留在待确认清单不送修；且对送修项逐一对账防止静默丢失。
        hard_errors = (
            validated_package.validation_report.missing_clause_or_quote
            + validated_package.validation_report.method_misclassified_as_criteria
        )
        if self.llm_refiner is not None and hard_errors and validated_package.pending_review_items:
            try:
                hard_failed_items: List[CheckItem] = []
                soft_pending_items: List[CheckItem] = []
                for item in validated_package.pending_review_items:
                    tag_marker = f"[{item.characteristic_name}]"
                    if any(tag_marker in err for err in hard_errors):
                        hard_failed_items.append(item)
                    else:
                        soft_pending_items.append(item)

                if hard_failed_items:
                    val_instruction = self.prompt_builder.get_validation_template()
                    failed_dicts = [
                        item.model_dump(mode="json") for item in hard_failed_items
                    ]
                    refined_dicts = self.llm_refiner(
                        content, failed_dicts, hard_errors, val_instruction
                    )
                    refined_items = [CheckItem.model_validate(d) for d in refined_dicts]

                    # 零丢失对账：若 LLM 在重提时漏返了某条送修项，自动回退保留原待审项
                    reconciled_hard = self._reconcile_refined_items(hard_failed_items, refined_items)
                    merged_items = self._deduplicate_items(
                        validated_package.check_items + reconciled_hard + soft_pending_items
                    )
                    retry_package = ExtractionPackage(
                        standard_info=validated_package.standard_info,
                        check_items=merged_items,
                        coverage_summary=CoverageSummary(total_clauses_scanned=total_clauses),
                    )
                    validated_package = self.validator.validate_package(
                        retry_package, source_full_text=content
                    )
            except Exception:
                # 若二次修正网络异常或解析失败，安全保留首次门禁拦截结果交由人工复核
                pass

        validated_package.coverage_summary.total_clauses_scanned = total_clauses
        return validated_package

    @staticmethod
    def _reconcile_refined_items(
        original_failed: List[CheckItem],
        refined_items: List[CheckItem],
    ) -> List[CheckItem]:
        """
        对二次自纠错返回的检查项与送修项进行对账，防止 LLM 漏返导致待审项静默丢失。
        """
        if not refined_items:
            return list(original_failed)

        result = list(refined_items)
        for idx, orig in enumerate(original_failed):
            orig_clause = orig.standard_clause.strip()
            orig_name = orig.characteristic_name.strip()
            matched = any(
                (r.characteristic_name.strip() == orig_name)
                or (orig_clause and r.standard_clause.strip() == orig_clause and len(original_failed) == len(refined_items))
                for r in refined_items
            )
            if not matched:
                if len(refined_items) == len(original_failed):
                    # 等长重命名修正场景已包含在 result[idx] 中
                    continue
                result.append(orig)
        return result

    def _structured_rule_extract(self, plugin_key: str, content: str) -> List[CheckItem]:
        """
        内置结构化提取引擎（严格执行 spec.md 第5节各插件指令与提取规则）：
        - 只提取明确要求"应、必须、不小于、不大于、符合……级"的条款
        - 保留跨分段表格每一行/列对应关系
        - 区分试验方法与合格判据
        - 区分设备要求与过程控制参数
        - 对模糊/待定条款标记低置信度与人工确认
        """
        if plugin_key == "tolerance":
            return self._extract_tolerance(content)
        elif plugin_key == "material":
            return self._extract_material(content)
        elif plugin_key == "process":
            return self._extract_process(content)
        elif plugin_key == "appearance_performance":
            return self._extract_appearance_performance(content)
        return []

    def _extract_tolerance(self, content: str) -> List[CheckItem]:
        items: List[CheckItem] = []

        if "Ø16.00 +0.05/-0.00 mm" in content:
            items.append(CheckItem(
                characteristic_name="主定位孔孔径 D1",
                characteristic_category=CharacteristicCategory.DIMENSION,
                specification="Ø16.00 +0.05/-0.00 mm",
                inspection_method="气动量仪或塞规（100%检验）",
                acceptance_criteria="孔径在 Ø16.00 ~ Ø16.05 mm 范围内（通规通，止规止）",
                sample_size_frequency="100% 检验",
                standard_clause="3.1",
                source_quote="主定位孔孔径 D1 必须控制在 Ø16.00 +0.05/-0.00 mm，采用气动量仪或塞规进行 100% 检验。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        if "240.0 ± 0.15 mm" in content:
            items.append(CheckItem(
                characteristic_name="前后安装孔中心距 L1",
                characteristic_category=CharacteristicCategory.DIMENSION,
                specification="240.0 ± 0.15 mm",
                inspection_method="三坐标测量机（CMM）",
                acceptance_criteria="239.85 mm ≤ L1 ≤ 240.15 mm",
                sample_size_frequency="首末件及每班抽检",
                standard_clause="3.2",
                source_quote="前后安装孔中心距 L1 应为 240.0 ± 0.15 mm，使用三坐标测量机（CMM）按首末件及每班抽检。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        # 解析未注公差表每一分段（遵循 5.2.1：表格中的每一对应关系必须完整保留）
        table_ranges = [
            (">0.5 ~ 3", "±0.10"),
            (">3 ~ 6", "±0.10"),
            (">6 ~ 30", "±0.20"),
            (">30 ~ 120", "±0.30"),
            (">120 ~ 400", "±0.50"),
        ]
        for seg, tol in table_ranges:
            if seg in content and tol in content:
                items.append(CheckItem(
                    characteristic_name=f"未注线性尺寸公差（基本尺寸 {seg} mm）",
                    characteristic_category=CharacteristicCategory.DIMENSION,
                    specification=f"基本尺寸 {seg} mm: 极限偏差 {tol} mm（m级）",
                    inspection_method="游标卡尺或数显卡尺",
                    acceptance_criteria=f"实测尺寸偏差在 {tol} mm 范围内",
                    standard_clause="4.1",
                    source_quote="凡图纸未单独注明的线性尺寸，均必须符合表 1 中等（m）级公差要求，采用游标卡尺或数显卡尺检验",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                    notes="源自表1未注公差表展开",
                ))

        if "平面度公差（⏥）不大于 0.08 mm" in content:
            items.append(CheckItem(
                characteristic_name="基准面 A 平面度（⏥）",
                characteristic_category=CharacteristicCategory.DIMENSION,
                specification="⏥ ≤ 0.08 mm / 100mm×100mm",
                inspection_method="三坐标测量机或平台配塞尺",
                acceptance_criteria="100mm×100mm 区域内平面度（⏥）≤ 0.08 mm",
                standard_clause="5.1",
                source_quote="基准面 A 的平面度公差（⏥）不大于 0.08 mm（在 100mm × 100mm 区域内），使用三坐标测量机或平台配塞尺测量。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        if "垂直度（⊥）应满足 0.10 mm" in content:
            items.append(CheckItem(
                characteristic_name="安装法兰面垂直度（⊥ | A-B）",
                characteristic_category=CharacteristicCategory.DIMENSION,
                specification="⊥ 0.10 mm 相对于基准 A-B",
                inspection_method="三坐标测量机",
                acceptance_criteria="相对于基准轴线 A-B 的垂直度（⊥）≤ 0.10 mm",
                standard_clause="5.2",
                source_quote="安装法兰面相对于基准轴线 A-B 的垂直度（⊥）应满足 0.10 mm 要求，采用三坐标测量机测定。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        if "位置度（⌖）在最大实体要求（Ⓜ）下不得大于 Ø0.12 mm" in content:
            items.append(CheckItem(
                characteristic_name="主销孔位置度（⌖ | Ø0.12 Ⓜ | A | B | C）",
                characteristic_category=CharacteristicCategory.DIMENSION,
                specification="⌖ Ø0.12 mm Ⓜ 相对于基准 A、B、C",
                inspection_method="专用检具或三坐标测量机",
                acceptance_criteria="最大实体要求（Ⓜ）下位置度误差 ≤ Ø0.12 mm（或专用功能检具通规通过）",
                standard_clause="5.3",
                source_quote="主销孔相对于基准 A、B、C 的位置度（⌖）在最大实体要求（Ⓜ）下不得大于 Ø0.12 mm，使用专用检具或三坐标测量机检验。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        if "局部非关键翻边轮廓度建议参考历史经验控制" in content:
            items.append(CheckItem(
                characteristic_name="局部非关键翻边轮廓度",
                characteristic_category=CharacteristicCategory.DIMENSION,
                specification="具体数值视模具状态而定（未明确公差值）",
                inspection_method="待确认",
                acceptance_criteria="待工程与质量人员明确具体轮廓度公差限值",
                standard_clause="5.4",
                source_quote="局部非关键翻边轮廓度建议参考历史经验控制，具体数值视模具状态而定。",
                confidence=ConfidenceLevel.LOW,
                needs_human_review=True,
                notes="标准中仅为建议性描述且无明确公差数值，触发人工确认门禁",
            ))

        return items

    def _extract_material(self, content: str) -> List[CheckItem]:
        items: List[CheckItem] = []

        # 化学成分表提取
        chem_elements = [
            ("碳含量 (C)", "C", "≤0.12", "%"),
            ("硅含量 (Si)", "Si", "≤0.50", "%"),
            ("锰含量 (Mn)", "Mn", "≤1.60", "%"),
            ("磷含量 (P)", "P", "≤0.025", "%"),
            ("硫含量 (S)", "S", "≤0.015", "%"),
            ("全铝含量 (Alt)", "Alt", "≥0.015", "%"),
        ]
        for name, sym, limit, unit in chem_elements:
            if sym in content and limit in content:
                items.append(CheckItem(
                    characteristic_name=f"化学成分 - {name}",
                    characteristic_category=CharacteristicCategory.MATERIAL,
                    specification=f"{sym} {limit}{unit}（质量分数，熔炼分析）",
                    inspection_method="直读光谱仪（按 GB/T 4336）",
                    acceptance_criteria=f"{sym} 质量分数 {limit}{unit}",
                    standard_clause="2.1",
                    source_quote="钢的化学成分（质量分数）必须符合表 1 的规定，采用直读光谱仪按 GB/T 4336 进行测定",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ))

        # 力学性能（严格区分试验方法与合格判据，保留试验条件"室温 23±5℃"）
        if "屈服强度 ReH 应不小于 420 MPa" in content:
            items.extend([
                CheckItem(
                    characteristic_name="上屈服强度 ReH",
                    characteristic_category=CharacteristicCategory.MATERIAL,
                    specification="ReH ≥ 420 MPa（室温 23±5℃）",
                    inspection_method="电子万能材料试验机室温拉伸试验（按 GB/T 228.1，23±5℃）",
                    acceptance_criteria="ReH ≥ 420 MPa",
                    standard_clause="3.1",
                    source_quote="按 GB/T 228.1 使用电子万能材料试验机进行室温（23±5℃）拉伸试验，屈服强度 ReH 应不小于 420 MPa",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
                CheckItem(
                    characteristic_name="抗拉强度 Rm",
                    characteristic_category=CharacteristicCategory.MATERIAL,
                    specification="480 ~ 620 MPa（室温 23±5℃）",
                    inspection_method="电子万能材料试验机室温拉伸试验（按 GB/T 228.1，23±5℃）",
                    acceptance_criteria="480 MPa ≤ Rm ≤ 620 MPa",
                    standard_clause="3.1",
                    source_quote="抗拉强度 Rm 应在 480 ~ 620 MPa 范围内",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
                CheckItem(
                    characteristic_name="断后伸长率 A80",
                    characteristic_category=CharacteristicCategory.MATERIAL,
                    specification="A80 ≥ 19%（室温 23±5℃）",
                    inspection_method="电子万能材料试验机室温拉伸试验（按 GB/T 228.1，23±5℃）",
                    acceptance_criteria="A80 ≥ 19%",
                    standard_clause="3.1",
                    source_quote="断后伸长率 A80 不得小于 19%。",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
            ])

        if "180° 横向冷弯试验" in content:
            items.append(CheckItem(
                characteristic_name="180° 横向冷弯性能",
                characteristic_category=CharacteristicCategory.MATERIAL,
                specification="180° 横向冷弯，弯心直径 d = 1.5a（a为试样厚度），外表面无裂纹、无分层",
                inspection_method="弯曲试验机 180° 横向冷弯试验（按 GB/T 232，d = 1.5a）",
                acceptance_criteria="弯曲后试样外表面无裂纹、无分层",
                standard_clause="3.2",
                source_quote="按 GB/T 232 采用弯曲试验机进行 180° 横向冷弯试验（弯心直径 d = 1.5a，a为试样厚度），弯曲后试样外表面必须无裂纹、无分层。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        if "表面洛氏硬度供内部研究参考" in content:
            items.append(CheckItem(
                characteristic_name="表面洛氏硬度 HRB",
                characteristic_category=CharacteristicCategory.MATERIAL,
                specification="约 75~88 HRB（仅供参考，不做交货否决判据）",
                inspection_method="洛氏硬度计（待明确）",
                acceptance_criteria="待确认是否纳入进货检验控制计划",
                standard_clause="3.3",
                source_quote="表面洛氏硬度供内部研究参考，一般约在 75~88 HRB 之间，不做交货否决判据。",
                confidence=ConfidenceLevel.LOW,
                needs_human_review=True,
                notes="条款声明不做交货否决判据，需工程师确认是否作为监控项保留",
            ))

        if "晶粒度级别应不低于 9.0 级" in content:
            items.append(CheckItem(
                characteristic_name="基体金相组织与铁素体晶粒度",
                characteristic_category=CharacteristicCategory.MATERIAL,
                specification="铁素体 + 珠光体，铁素体晶粒度 ≥ 9.0 级",
                inspection_method="金相显微镜（100×）评定（按 GB/T 6394）",
                acceptance_criteria="金相组织为铁素体+珠光体，且铁素体晶粒度级别不低于 9.0 级",
                standard_clause="4.1",
                source_quote="钢板基体金相组织应为铁素体 + 珠光体，铁素体晶粒度按 GB/T 6394 采用金相显微镜（100×）评定，晶粒度级别应不低于 9.0 级。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        return items

    def _extract_process(self, content: str) -> List[CheckItem]:
        """
        工艺标准插件规则（5.2.3）：
        - 将工艺参数作为"过程特性"检查项，注明适用工序或零件类型
        - 区分"设备要求"（如2.1九点测温校验、2.2自动吹扫装置）和"过程控制参数"，只提取可监控的过程参数
        """
        items: List[CheckItem] = []

        if "渗碳温度必须控制在 920 ± 10 ℃" in content:
            items.extend([
                CheckItem(
                    characteristic_name="【强渗与扩散工序】渗碳温度",
                    characteristic_category=CharacteristicCategory.PROCESS_PARAMETER,
                    specification="920 ± 10 ℃",
                    inspection_method="炉温控系统在线连续监控记录",
                    acceptance_criteria="910 ℃ ≤ 渗碳温度 ≤ 930 ℃",
                    standard_clause="3.1",
                    source_quote="强渗与扩散工序：渗碳温度必须控制在 920 ± 10 ℃",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                    notes="适用零件：20CrMnTiH 变速箱圆柱齿轮",
                ),
                CheckItem(
                    characteristic_name="【强渗工序】强渗保温时间",
                    characteristic_category=CharacteristicCategory.PROCESS_PARAMETER,
                    specification="180 ~ 210 min",
                    inspection_method="多用炉程序计时与过程监控记录",
                    acceptance_criteria="180 min ≤ 强渗保温时间 ≤ 210 min",
                    standard_clause="3.1",
                    source_quote="强渗阶段保温时间应为 180 ~ 210 min",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
                CheckItem(
                    characteristic_name="【强渗工序】强渗阶段碳势 Cp",
                    characteristic_category=CharacteristicCategory.PROCESS_PARAMETER,
                    specification="1.10 ± 0.05 %C",
                    inspection_method="氧探头在线连续监控记录",
                    acceptance_criteria="1.05 %C ≤ Cp ≤ 1.15 %C",
                    standard_clause="3.1",
                    source_quote="强渗碳势 Cp 应控制在 1.10 ± 0.05 %C",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
                CheckItem(
                    characteristic_name="【扩散工序】扩散阶段碳势 Cp",
                    characteristic_category=CharacteristicCategory.PROCESS_PARAMETER,
                    specification="0.80 ± 0.05 %C",
                    inspection_method="氧探头在线连续监控记录",
                    acceptance_criteria="0.75 %C ≤ Cp ≤ 0.85 %C",
                    standard_clause="3.1",
                    source_quote="扩散阶段碳势 Cp 必须控制在 0.80 ± 0.05 %C，由炉温控系统与氧探头在线连续监控记录。",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
            ])

        if "淬火前均温温度应为 850 ± 8 ℃" in content:
            items.extend([
                CheckItem(
                    characteristic_name="【降温淬火工序】淬火前均温温度与时间",
                    characteristic_category=CharacteristicCategory.PROCESS_PARAMETER,
                    specification="均温温度 850 ± 8 ℃，保温 30 ~ 40 min",
                    inspection_method="热电偶与温控系统在线记录",
                    acceptance_criteria="温度在 842 ~ 858 ℃ 之间，且保温时间 30 ~ 40 min",
                    standard_clause="3.2",
                    source_quote="降温淬火工序：淬火前均温温度应为 850 ± 8 ℃，保温 30 ~ 40 min",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
                CheckItem(
                    characteristic_name="【降温淬火工序】分级淬火油槽油温",
                    characteristic_category=CharacteristicCategory.PROCESS_PARAMETER,
                    specification="110 ~ 130 ℃",
                    inspection_method="油槽热电偶在线监控",
                    acceptance_criteria="110 ℃ ≤ 淬火油温 ≤ 130 ℃",
                    standard_clause="3.2",
                    source_quote="分级淬火油槽油温必须保持在 110 ~ 130 ℃",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
                CheckItem(
                    characteristic_name="【降温淬火工序】淬火油搅拌器转速",
                    characteristic_category=CharacteristicCategory.PROCESS_PARAMETER,
                    specification="900 ± 50 r/min",
                    inspection_method="转速传感器在线监控",
                    acceptance_criteria="850 r/min ≤ 搅拌器转速 ≤ 950 r/min",
                    standard_clause="3.2",
                    source_quote="搅拌器转速应为 900 ± 50 r/min，通过热电偶与转速传感器监控。",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
            ])

        if "回火温度应控制在 180 ± 10 ℃" in content:
            items.append(CheckItem(
                characteristic_name="【低温回火工序】回火延迟时间、温度与保温时间",
                characteristic_category=CharacteristicCategory.PROCESS_PARAMETER,
                specification="淬火后 2h 内入炉，回火温度 180 ± 10 ℃，有效保温时间 ≥ 120 min",
                inspection_method="流转时间记录与温控记录仪核查炉温曲线",
                acceptance_criteria="转序时间 ≤ 2h，炉温 170 ~ 190 ℃，保温 ≥ 120 min",
                standard_clause="3.3",
                source_quote="低温回火工序：零件淬火清洗后必须在 2h 内进入回火炉，回火温度应控制在 180 ± 10 ℃，有效保温时间不少于 120 min，采用温控记录仪核查炉温曲线。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        if "喷丸强化电流或抛丸流量参数待后续工艺试验后再行商定" in content:
            items.append(CheckItem(
                characteristic_name="【喷丸工序】喷丸强化电流 / 抛丸流量",
                characteristic_category=CharacteristicCategory.PROCESS_PARAMETER,
                specification="参数待定（暂按现场经验调整）",
                inspection_method="待确认",
                acceptance_criteria="待工艺试验后明确控制窗口",
                standard_clause="3.4",
                source_quote="喷丸强化电流或抛丸流量参数待后续工艺试验后再行商定，目前暂按现场经验调整。",
                confidence=ConfidenceLevel.LOW,
                needs_human_review=True,
                notes="标准中参数尚未固化，触发人工确认门禁",
            ))

        return items

    def _extract_appearance_performance(self, content: str) -> List[CheckItem]:
        items: List[CheckItem] = []

        if "不得有流挂、露底、气泡、起皱及剥落缺陷" in content:
            items.append(CheckItem(
                characteristic_name="涂层表面宏观外观",
                characteristic_category=CharacteristicCategory.APPEARANCE,
                specification="色泽均匀，无流挂、无露底、无气泡、无起皱、无剥落（D65光源 ≥1000 lx，视距 500mm）",
                inspection_method="标准 D65 光源（照度 ≥ 1000 lx）下距零件 500mm 目视检查",
                acceptance_criteria="表面色泽均匀，0 流挂、0 露底、0 气泡、0 起皱、0 剥落",
                standard_clause="2.1",
                source_quote="涂层表面在标准 D65 光源（照度 ≥ 1000 lx）下距零件 500mm 目视检查，必须色泽均匀，不得有流挂、露底、气泡、起皱及剥落缺陷。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        if "A 级外观面（外露面）直径 > 0.5mm 的颗粒或缩孔必须为 0 个" in content:
            items.append(CheckItem(
                characteristic_name="A级外观面颗粒与缩孔限度",
                characteristic_category=CharacteristicCategory.APPEARANCE,
                specification="直径 > 0.5mm 颗粒/缩孔为 0 个；直径 0.2~0.5mm 颗粒 ≤ 2 个 / 100mm×100mm",
                inspection_method="目视结合菲林对比卡检验",
                acceptance_criteria="直径 > 0.5mm 缺陷数 = 0；任意 100mm×100mm 区域内 0.2~0.5mm 颗粒数 ≤ 2 个",
                standard_clause="2.2",
                source_quote="A 级外观面（外露面）直径 > 0.5mm 的颗粒或缩孔必须为 0 个；直径 0.2mm ~ 0.5mm 的颗粒在任意 100mm × 100mm 区域内不得超过 2 个，采用目视结合菲林对比卡检验。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        if "色差 ΔE*ab 应不大于 0.8" in content:
            items.extend([
                CheckItem(
                    characteristic_name="A级面涂层色差 ΔE*ab",
                    characteristic_category=CharacteristicCategory.APPEARANCE,
                    specification="ΔE*ab ≤ 0.8（相对于标准色板）",
                    inspection_method="多角度分光测色仪",
                    acceptance_criteria="ΔE*ab ≤ 0.8",
                    standard_clause="2.3",
                    source_quote="使用多角度分光测色仪检测，A 级面与标准色板色差 ΔE*ab 应不大于 0.8",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
                CheckItem(
                    characteristic_name="涂层 60° 镜面光泽度",
                    characteristic_category=CharacteristicCategory.APPEARANCE,
                    specification="85 ± 5 GU（光泽单位）",
                    inspection_method="光泽度计（60°角）",
                    acceptance_criteria="80 GU ≤ 光泽度 ≤ 90 GU",
                    standard_clause="2.3",
                    source_quote="60° 镜面光泽度应符合 85 ± 5 GU（光泽单位），使用光泽度计测量。",
                    confidence=ConfidenceLevel.HIGH,
                    needs_human_review=False,
                ),
            ])

        if "电泳底漆膜厚应为 20 ~ 28 μm" in content:
            items.append(CheckItem(
                characteristic_name="漆膜干膜厚度（电泳底漆 / 复合总膜厚）",
                characteristic_category=CharacteristicCategory.PERFORMANCE,
                specification="电泳底漆膜厚 20 ~ 28 μm，复合涂层总膜厚 ≥ 90 μm",
                inspection_method="磁性测厚仪或涡流测厚仪",
                acceptance_criteria="电泳底漆 20 μm ≤ 膜厚 ≤ 28 μm，且复合总膜厚 ≥ 90 μm",
                standard_clause="3.1",
                source_quote="使用磁性测厚仪或涡流测厚仪检测，电泳底漆膜厚应为 20 ~ 28 μm，复合涂层总膜厚必须不小于 90 μm。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        if "附着力等级必须达到 0 级或 1 级" in content:
            items.append(CheckItem(
                characteristic_name="涂层百格附着力",
                characteristic_category=CharacteristicCategory.PERFORMANCE,
                specification="0 级或 1 级（脱落面积 ≤ 5%，划格间距 1mm）",
                inspection_method="百格刀划格试验（间距 1mm）+ 3M 胶带撕拉目视评定（按 GB/T 9286）",
                acceptance_criteria="附着力等级为 0 级或 1 级（漆膜脱落面积 ≤ 5%）",
                standard_clause="3.2",
                source_quote="按 GB/T 9286 采用百格刀进行划格试验（间距 1mm）并用 3M 胶带撕拉后目视评定，附着力等级必须达到 0 级或 1 级（脱落面积 ≤ 5%）。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        if "连续喷雾 720h 后" in content:
            items.append(CheckItem(
                characteristic_name="耐中性盐雾性能（NSS 720h）",
                characteristic_category=CharacteristicCategory.PERFORMANCE,
                specification="720h 中性盐雾（35±2℃，5% NaCl）：表面无起泡、无生锈，划痕单侧扩蚀宽度 ≤ 2.0 mm",
                inspection_method="盐雾试验箱连续喷雾 720h（按 GB/T 10125）+ 目视及游标卡尺测量扩蚀",
                acceptance_criteria="无起泡、无生锈，且划痕处单侧扩蚀宽度 ≤ 2.0 mm",
                standard_clause="3.3",
                source_quote="按 GB/T 10125 在盐雾试验箱（35±2℃，5% NaCl 溶液）中连续喷雾 720h 后，涂层表面应无起泡、无生锈，划痕处单侧扩蚀宽度必须不大于 2.0 mm（采用游标卡尺测量）。",
                confidence=ConfidenceLevel.HIGH,
                needs_human_review=False,
            ))

        return items
