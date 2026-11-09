"""
Pydantic 数据模型定义，严格遵循 spec.md 第6节输出规范。
"""

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator


class CharacteristicCategory(str, Enum):
    """特性类别枚举"""
    DIMENSION = "尺寸"
    MATERIAL = "材料"
    PROCESS_PARAMETER = "工艺参数"
    APPEARANCE = "外观"
    PERFORMANCE = "性能"


class ConfidenceLevel(str, Enum):
    """置信度枚举"""
    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"


class StandardCategory(str, Enum):
    """标准分类枚举"""
    TOLERANCE = "公差/尺寸标准"
    MATERIAL = "材料标准"
    PROCESS = "工艺标准"
    APPEARANCE_PERFORMANCE = "外观/性能标准"


class CheckItem(BaseModel):
    """单条控制计划检查项数据模型（v2.0 15字段 IATF 16949 标准契约）"""
    characteristic_name: str = Field(..., description="特性名称（控制计划术语）")
    characteristic_category: CharacteristicCategory = Field(..., description="特性类别：尺寸/材料/工艺参数/外观/性能")
    special_char_class: Optional[str] = Field(default=None, description="特殊特性分类（如 CC/SC/PTC/关键特性/重要特性，未明确时为 null）")
    specification: str = Field(..., description="规范或公差，保留原文数值、单位、判定描述")
    inspection_method: str = Field(..., description="检查方法，如卡尺、拉伸试验、目视、盐雾试验")
    acceptance_criteria: str = Field(..., description="判定准则，合格/不合格的明确条件")
    sample_size_frequency: Optional[str] = Field(default=None, description="样本容量与抽样频次（如 100%、5件/每班、每批首末件，未明确时为 null）")
    control_method: Optional[str] = Field(default=None, description="控制方法（如 SPC控制图、首末件记录表、防错点检表、在线连续监控，未明确时为 null）")
    reaction_plan: Optional[str] = Field(default=None, description="反应计划（如 立即停机隔离、标识不良品并100%追溯，未明确时为 null）")
    standard_clause: str = Field(..., description="标准条款号或章节号")
    source_page: Optional[int] = Field(default=None, ge=1, description="标准条款或原文所在的 PDF 页码（从 1 开始）")
    source_quote: str = Field(..., description="原文引用摘录")
    confidence: ConfidenceLevel = Field(..., description="提取置信度：高/中/低")
    needs_human_review: bool = Field(..., description="是否需人工确认")
    notes: Optional[str] = Field(default=None, description="备注说明")

    @model_validator(mode="after")
    def enforce_low_confidence_review(self) -> "CheckItem":
        # 规格门禁：低置信度项必须标记 needs_human_review = True
        if self.confidence == ConfidenceLevel.LOW and not self.needs_human_review:
            self.needs_human_review = True
        return self


class StandardInfo(BaseModel):
    """标准基本元信息"""
    file_name: str = Field(..., description="标准文件名")
    standard_category: str = Field(..., description="标准分类名称或标识")
    template_version: str = Field(default="v1.0", description="所用提取模板版本")
    source_file_url: Optional[str] = Field(default=None, description="源标准文件（如 PDF）在线预览 URL，供条款超链接跳转")


class CoverageSummary(BaseModel):
    """覆盖率与提取统计摘要"""
    total_clauses_scanned: int = Field(default=0, ge=0, description="扫描标准条款总数")
    check_items_extracted: int = Field(default=0, ge=0, description="提取检查项总数")
    high_confidence: int = Field(default=0, ge=0, description="高置信度项数量")
    medium_confidence: int = Field(default=0, ge=0, description="中置信度项数量")
    low_confidence: int = Field(default=0, ge=0, description="低置信度项数量")


class ValidationReport(BaseModel):
    """后处理校验报告模型"""
    missing_clause_or_quote: List[str] = Field(default_factory=list, description="缺失条款或原文引用的项")
    unit_inconsistency: List[str] = Field(default_factory=list, description="单位不一致或缺失的项")
    method_misclassified_as_criteria: List[str] = Field(default_factory=list, description="将试验方法误写为判定准则的项")
    unmarked_low_confidence: List[str] = Field(default_factory=list, description="未正确标记人工审核的低置信度项")
    suggestions: List[str] = Field(default_factory=list, description="整改建议或优化提示")


class ExtractionPackage(BaseModel):
    """完整输出包结构"""
    standard_info: StandardInfo
    check_items: List[CheckItem] = Field(default_factory=list, description="正式提取的检查项列表")
    pending_review_items: List[CheckItem] = Field(default_factory=list, description="待确认清单（需人工确认或低置信度项）")
    coverage_summary: CoverageSummary = Field(default_factory=CoverageSummary)
    validation_report: ValidationReport = Field(default_factory=ValidationReport)

    def refresh_summary(self) -> None:
        """更新覆盖率统计数据"""
        all_items = self.check_items + [
            item for item in self.pending_review_items if item not in self.check_items
        ]
        self.coverage_summary.check_items_extracted = len(all_items)
        self.coverage_summary.high_confidence = sum(1 for i in all_items if i.confidence == ConfidenceLevel.HIGH)
        self.coverage_summary.medium_confidence = sum(1 for i in all_items if i.confidence == ConfidenceLevel.MEDIUM)
        self.coverage_summary.low_confidence = sum(1 for i in all_items if i.confidence == ConfidenceLevel.LOW)


# ==================== 仅特性清单模式 (Characteristic List Mode, v2.1) ====================


class CharacteristicEntry(BaseModel):
    """单条特性清单条目（仅特性清单模式：只关心「有哪些特性」，不提取规范/方法/判据）"""
    characteristic_name: str = Field(..., description="中文控制计划规范特性名称")
    original_name: str = Field(..., description="特性在原文中的名称（逐字摘录，必须为 source_quote 子串）")
    standard_clauses: List[str] = Field(default_factory=list, description="出现该特性的全部标准条款号")
    source_quote: str = Field(..., description="原文引用逐字摘录（防幻觉溯源锚点）")
    source_page: Optional[int] = Field(default=None, ge=1, description="原文所在 PDF 页码（从 1 开始）")
    is_mandatory: bool = Field(default=True, description="是否为强制要求（建议性/无判据条款为 false）")
    confidence: ConfidenceLevel = Field(..., description="提取置信度：高/中/低")
    needs_human_review: bool = Field(default=False, description="是否需人工确认")
    notes: Optional[str] = Field(default=None, description="备注说明或门禁拦截提示")

    @model_validator(mode="after")
    def enforce_low_confidence_review(self) -> "CharacteristicEntry":
        if self.confidence == ConfidenceLevel.LOW and not self.needs_human_review:
            self.needs_human_review = True
        return self


class CharacteristicListReport(BaseModel):
    """仅特性清单模式的精简校验报告"""
    missing_clause_or_quote: List[str] = Field(default_factory=list, description="缺失条款/原文引用或原文无法匹配的项")
    name_not_in_quote: List[str] = Field(default_factory=list, description="原文名称不在原文引用中的项")
    suggestions: List[str] = Field(default_factory=list, description="整改建议或优化提示")


class CharacteristicListPackage(BaseModel):
    """仅特性清单模式输出包"""
    standard_info: StandardInfo
    characteristics: List[CharacteristicEntry] = Field(default_factory=list, description="通过门禁的特性清单")
    pending_review_items: List[CharacteristicEntry] = Field(default_factory=list, description="待人工确认的特性")
    coverage_summary: CoverageSummary = Field(default_factory=CoverageSummary)
    validation_report: CharacteristicListReport = Field(default_factory=CharacteristicListReport)

    def refresh_summary(self) -> None:
        all_items = self.characteristics + self.pending_review_items
        self.coverage_summary.check_items_extracted = len(all_items)
        self.coverage_summary.high_confidence = sum(1 for i in all_items if i.confidence == ConfidenceLevel.HIGH)
        self.coverage_summary.medium_confidence = sum(1 for i in all_items if i.confidence == ConfidenceLevel.MEDIUM)
        self.coverage_summary.low_confidence = sum(1 for i in all_items if i.confidence == ConfidenceLevel.LOW)
