"""
控制计划与 Excel 导出映射器
负责将校验/审核后的结构化检查项导出为符合汽车行业控制计划习惯的 Excel 清单。
"""

import os
from pathlib import Path
from typing import Union
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from ..schemas.models import CharacteristicListPackage, ExtractionPackage


class ControlPlanExcelMapper:
    """控制计划 Excel 映射与导出类（v2.0 15列 IATF 16949 标准布局）"""

    DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8000"

    HEADER_FILL = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    HEADER_FONT = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
    WARNING_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    BORDER_THIN = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    COLUMNS = [
        ("序号", 8),
        ("特性名称", 24),
        ("特性类别", 14),
        ("规范 / 公差", 28),
        ("检查 / 试验方法", 22),
        ("判定准则", 26),
        ("标准条款号", 14),
        ("原文引用摘录", 36),
        ("特殊特性分类", 14),
        ("样本容量 / 频次", 18),
        ("控制方法", 22),
        ("反应计划", 24),
        ("置信度", 10),
        ("需人工确认", 12),
        ("备注", 20),
    ]
    # 居中对齐的列序号与条款号超链接所在列序号（子类可覆写）
    CENTER_COLS = (1, 3, 7, 9, 13, 14)
    CLAUSE_COL = 7

    def export(self, package: ExtractionPackage, output_path: Union[str, Path]) -> Path:
        """导出提取包到 Excel 文件"""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()
        # 默认工作表改为控制计划清单
        ws_items = wb.active
        ws_items.title = "控制计划检查项"

        # 1. 渲染正式检查项工作表
        self._write_sheet_headers(ws_items, f"控制计划检查项清单 - {package.standard_info.file_name}")
        source_url = package.standard_info.source_file_url
        row_idx = 3
        for idx, item in enumerate(package.check_items, start=1):
            clause_label = (
                f"{item.standard_clause} (P.{item.source_page})"
                if item.source_page
                else item.standard_clause
            )
            ws_items.append([
                idx,
                item.characteristic_name,
                item.characteristic_category.value,
                item.specification,
                item.inspection_method,
                item.acceptance_criteria,
                clause_label,
                item.source_quote,
                item.special_char_class or "",
                item.sample_size_frequency or "",
                item.control_method or "",
                item.reaction_plan or "",
                item.confidence.value,
                "是" if item.needs_human_review else "否",
                item.notes or "",
            ])
            self._style_data_row(ws_items, row_idx)
            self._apply_clause_hyperlink(ws_items, row_idx, source_url, item.source_page)
            row_idx += 1

        # 2. 渲染待人工确认工作表
        ws_pending = wb.create_sheet(title="待人工确认项 (门禁)")
        self._write_sheet_headers(ws_pending, f"待审核/低置信度清单 - {package.standard_info.file_name}")
        row_idx = 3
        for idx, item in enumerate(package.pending_review_items, start=1):
            clause_label = (
                f"{item.standard_clause} (P.{item.source_page})"
                if item.source_page
                else item.standard_clause
            )
            ws_pending.append([
                idx,
                item.characteristic_name,
                item.characteristic_category.value,
                item.specification,
                item.inspection_method,
                item.acceptance_criteria,
                clause_label,
                item.source_quote,
                item.special_char_class or "",
                item.sample_size_frequency or "",
                item.control_method or "",
                item.reaction_plan or "",
                item.confidence.value,
                "是" if item.needs_human_review else "否",
                item.notes or "",
            ])
            self._style_data_row(ws_pending, row_idx, highlight_warning=True)
            self._apply_clause_hyperlink(ws_pending, row_idx, source_url, item.source_page)
            row_idx += 1

        # 3. 渲染摘要与校验报告工作表
        ws_report = wb.create_sheet(title="覆盖率与校验报告")
        self._write_report_sheet(ws_report, package)

        wb.save(output_path)
        return output_path

    def _write_sheet_headers(self, ws, title: str):
        # 标题行
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(self.COLUMNS))
        top_cell = ws.cell(row=1, column=1, value=title)
        top_cell.font = Font(name="微软雅黑", size=14, bold=True, color="1F497D")
        top_cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[1].height = 30

        # 列头行
        ws.row_dimensions[2].height = 24
        for col_idx, (col_name, col_width) in enumerate(self.COLUMNS, start=1):
            cell = ws.cell(row=2, column=col_idx, value=col_name)
            cell.font = self.HEADER_FONT
            cell.fill = self.HEADER_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center")
            col_letter = openpyxl.utils.get_column_letter(col_idx)
            ws.column_dimensions[col_letter].width = col_width

    def _style_data_row(self, ws, row_idx: int, highlight_warning: bool = False):
        ws.row_dimensions[row_idx].height = 22
        font = Font(name="微软雅黑", size=10)
        align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)
        align_center = Alignment(horizontal="center", vertical="center")

        for col_idx in range(1, len(self.COLUMNS) + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.font = font
            cell.border = self.BORDER_THIN
            if highlight_warning:
                cell.fill = self.WARNING_FILL
            if col_idx in self.CENTER_COLS:
                cell.alignment = align_center
            else:
                cell.alignment = align_left

    @classmethod
    def resolve_full_hyperlink_url(cls, source_url: str | None, source_page: int | None) -> str | None:
        """
        将根相对路径（如 /api/source/xxx.pdf）补全为带完整 HTTP 协议与主机头的绝对 URL，
        确保下载后的 Excel 在本地通过 Office/WPS 点击超链接时能直接唤起浏览器并精准翻到 #page=N 页。
        """
        if not source_url:
            return None
        cleaned = source_url.strip()
        if cleaned.startswith("/"):
            base_origin = os.getenv("PUBLIC_BASE_URL", cls.DEFAULT_PUBLIC_BASE_URL).strip().rstrip("/")
            resolved_url = f"{base_origin}{cleaned}"
        else:
            resolved_url = cleaned

        page_num = source_page or 1
        return f"{resolved_url}#page={page_num}" if resolved_url.lower().endswith(".pdf") else resolved_url

    def _apply_clause_hyperlink(self, ws, row_idx: int, source_url: str | None, source_page: int | None):
        """若存在源文件链接（如 PDF），将第 7 列（标准条款号）设置为带完整 HTTP 基地址与 #page=N 锚点的超链接"""
        target_link = self.resolve_full_hyperlink_url(source_url, source_page)
        if not target_link:
            return
        cell = ws.cell(row=row_idx, column=self.CLAUSE_COL)
        cell.hyperlink = target_link
        cell.font = Font(name="微软雅黑", size=10, color="0563C1", underline="single")

    def _write_report_sheet(self, ws, package: ExtractionPackage):
        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 60

        ws.append(["标准提审与校验报告", ""])
        ws.merge_cells("A1:B1")
        ws.cell(1, 1).font = Font(name="微软雅黑", size=14, bold=True, color="1F497D")

        summary = package.coverage_summary
        ws.append(["标准文件名", package.standard_info.file_name])
        ws.append(["标准分类", package.standard_info.standard_category])
        ws.append(["模板版本", package.standard_info.template_version])
        ws.append(["提取检查项总数", summary.check_items_extracted])
        ws.append(["高置信度项数", summary.high_confidence])
        ws.append(["中置信度项数", summary.medium_confidence])
        ws.append(["低置信度项数（需确认）", summary.low_confidence])
        ws.append(["待审核门禁项数", len(package.pending_review_items)])

        ws.append(["", ""])
        ws.append(["校验告警与建议", ""])
        header_row = ws.max_row
        ws.cell(header_row, 1).font = Font(name="微软雅黑", size=12, bold=True)

        report = package.validation_report
        for item in report.missing_clause_or_quote:
            ws.append(["溯源缺失告警", item])
        for item in report.method_misclassified_as_criteria:
            ws.append(["方法/准则混淆告警", item])
        for item in report.unit_inconsistency:
            ws.append(["单位不一致告警", item])
        for item in report.suggestions:
            ws.append(["优化建议", item])


class CharacteristicListExcelMapper(ControlPlanExcelMapper):
    """仅特性清单模式 Excel 导出（精简 7 列）"""

    COLUMNS = [
        ("序号", 8),
        ("特性名称 (中文规范名)", 28),
        ("原文名称", 26),
        ("是否强制", 10),
        ("置信度", 10),
        ("标准条款号", 18),
        ("原文引用摘录", 48),
        ("待确认", 10),
        ("备注", 28),
    ]
    CENTER_COLS = (1, 4, 5, 6, 8)
    CLAUSE_COL = 6

    def _append_entries(self, ws, entries, source_url, highlight_warning: bool = False):
        row_idx = 3
        for idx, item in enumerate(entries, start=1):
            clauses = "、".join(item.standard_clauses) or "-"
            clause_label = f"{clauses} (P.{item.source_page})" if item.source_page else clauses
            ws.append([
                idx,
                item.characteristic_name,
                item.original_name,
                "是" if item.is_mandatory else "否",
                item.confidence.value,
                clause_label,
                item.source_quote,
                "是" if item.needs_human_review else "否",
                item.notes or "",
            ])
            self._style_data_row(ws, row_idx, highlight_warning=highlight_warning)
            self._apply_clause_hyperlink(ws, row_idx, source_url, item.source_page)
            row_idx += 1

    def export(self, package: CharacteristicListPackage, output_path: Union[str, Path]) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        source_url = package.standard_info.source_file_url

        wb = openpyxl.Workbook()
        ws_items = wb.active
        ws_items.title = "特性清单"
        self._write_sheet_headers(ws_items, f"特性清单 - {package.standard_info.file_name}")
        self._append_entries(ws_items, package.characteristics, source_url)

        ws_pending = wb.create_sheet(title="待人工确认特性")
        self._write_sheet_headers(ws_pending, f"待人工确认特性 - {package.standard_info.file_name}")
        self._append_entries(ws_pending, package.pending_review_items, source_url, highlight_warning=True)

        ws_report = wb.create_sheet(title="校验报告")
        ws_report.column_dimensions["A"].width = 24
        ws_report.column_dimensions["B"].width = 70
        ws_report.append(["特性清单校验报告", ""])
        ws_report.cell(1, 1).font = Font(name="微软雅黑", size=14, bold=True, color="1F497D")
        summary = package.coverage_summary
        ws_report.append(["标准文件名", package.standard_info.file_name])
        ws_report.append(["标准分类", package.standard_info.standard_category])
        ws_report.append(["模板版本", package.standard_info.template_version])
        ws_report.append(["特性总数", summary.check_items_extracted])
        ws_report.append(["强制特性数", sum(1 for i in package.characteristics + package.pending_review_items if i.is_mandatory)])
        ws_report.append(["待人工确认数", len(package.pending_review_items)])
        ws_report.append(["", ""])
        report = package.validation_report
        for msg in report.missing_clause_or_quote:
            ws_report.append(["溯源失配告警", msg])
        for msg in report.name_not_in_quote:
            ws_report.append(["原文名称告警", msg])
        for msg in report.suggestions:
            ws_report.append(["优化建议", msg])

        wb.save(output_path)
        return output_path
