"""
文档解析模块
支持将上传的：
- 截图/图片 (.png / .jpg / .jpeg / .webp / .bmp / .tiff / .heic) -> 调用 macOS Vision 高精度中英混合 OCR（含表格同行对齐）
- PDF (.pdf) -> 优先提取原生文本层；若为扫描件/图片型 PDF 则自动无缝回退至 OCR 识别
- Word (.docx) -> 提取段落并将表格转换为 Markdown 表格
- Markdown / 纯文本 (.md / .txt)
转换为结构化文本。
"""

import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List
from pypdf import PdfReader
import docx

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
VISION_OCR_BIN = ROOT_DIR / "scripts" / "vision_ocr"
VISION_OCR_SRC = ROOT_DIR / "scripts" / "vision_ocr.swift"

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".heic", ".gif")


class DocumentParser:
    """多格式标准文档与截图解析器（内置 OCR 与表格结构还原支持）"""

    @staticmethod
    def parse_bytes(filename: str, data: bytes) -> str:
        ext = Path(filename).suffix.lower()
        if ext in (".md", ".txt", ".markdown"):
            return data.decode("utf-8", errors="replace")
        elif ext == ".pdf":
            return DocumentParser._parse_pdf(data)
        elif ext == ".docx":
            return DocumentParser._parse_docx(data)
        elif ext in IMAGE_EXTENSIONS:
            return DocumentParser._parse_image(data, ext)
        else:
            raise ValueError(
                f"暂不支持的文件格式: {ext}（支持截图 .png/.jpg/.webp、.pdf、.docx、.md、.txt）"
            )

    @staticmethod
    def _format_space_aligned_tables(text: str) -> str:
        """
        将 PDF 布局模式提取出的多空格对齐表格行自动识别并转换为 Markdown 表格 (`| col1 | col2 |`)，
        确保公差矩阵表与化学成分表的行/列对应关系在送入 LLM 时不丢失。
        """
        lines = text.splitlines()
        output_lines: List[str] = []
        table_buffer: List[List[str]] = []

        def flush_table() -> None:
            nonlocal table_buffer
            if len(table_buffer) >= 2:
                max_cols = max(len(r) for r in table_buffer)
                for r_idx, row in enumerate(table_buffer):
                    padded = row + [""] * (max_cols - len(row))
                    output_lines.append("| " + " | ".join(padded) + " |")
                    if r_idx == 0:
                        output_lines.append("| " + " | ".join(["---"] * max_cols) + " |")
            else:
                for row in table_buffer:
                    output_lines.append("  ".join(row))
            table_buffer = []

        for line in lines:
            stripped = line.strip()
            # 若已经是 Markdown 表格行或章节标题，先刷新缓冲区
            if stripped.startswith("|") or stripped.startswith("#") or not stripped:
                if table_buffer:
                    flush_table()
                output_lines.append(line)
                continue

            # 识别以 2 个及以上连续空格或制表符分隔的多列数据行（至少 3 列，或 2 列且含数值/符号）
            cols = [c.strip() for c in re.split(r"\t+|\s{2,}", stripped) if c.strip()]
            is_table_row = len(cols) >= 3 or (
                len(cols) == 2 and bool(re.search(r"[0-9±≤≥~～%Øφ]", stripped))
            )
            if is_table_row and not re.match(r"^\d+\.\d+\s+", stripped):
                table_buffer.append(cols)
            else:
                if table_buffer:
                    flush_table()
                output_lines.append(line)

        if table_buffer:
            flush_table()

        return "\n".join(output_lines)

    @staticmethod
    def _parse_pdf(data: bytes) -> str:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for idx, page in enumerate(reader.pages, start=1):
            try:
                # 优先启用 layout 模式保留 PDF 表格的同行多列空间对齐关系
                txt = page.extract_text(extraction_mode="layout") or ""
            except Exception:
                txt = page.extract_text() or ""
            if txt.strip():
                formatted_txt = DocumentParser._format_space_aligned_tables(txt.strip())
                pages.append(f"<!-- 第 {idx} 页 -->\n{formatted_txt}")

        combined = "\n\n".join(pages)
        # 若原生 PDF 提取出的有效字符极少（如扫描版/截图合成 PDF），自动启动 OCR 识别
        if len(combined.strip()) < 20:
            ocr_text = DocumentParser._run_ocr(data, suffix=".pdf")
            if ocr_text.strip():
                return DocumentParser._format_space_aligned_tables(ocr_text)
        return combined

    @staticmethod
    def _parse_image(data: bytes, ext: str = ".png") -> str:
        ocr_text = DocumentParser._run_ocr(data, suffix=ext or ".png")
        if not ocr_text.strip():
            raise ValueError("未能从截图中识别出文字，请确认图片清晰且包含标准文本内容。")
        return f"<!-- 标准截图 OCR 识别内容 -->\n{ocr_text.strip()}"

    @staticmethod
    def _ensure_vision_binary() -> Path | None:
        if VISION_OCR_BIN.exists():
            return VISION_OCR_BIN
        if VISION_OCR_SRC.exists() and shutil.which("swiftc"):
            try:
                subprocess.run(
                    ["swiftc", "-O", str(VISION_OCR_SRC), "-o", str(VISION_OCR_BIN)],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                return VISION_OCR_BIN
            except Exception:
                return None
        return None

    @staticmethod
    def _run_ocr(data: bytes, suffix: str = ".png") -> str:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)

        try:
            # 1. 优先使用 macOS Apple Vision 原生高精度中英混合 OCR（自动按行对齐表格）
            bin_path = DocumentParser._ensure_vision_binary()
            if bin_path is not None:
                res = subprocess.run(
                    [str(bin_path), str(tmp_path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.strip()

            # 2. 备选：调用系统 tesseract CLI
            if shutil.which("tesseract") and suffix != ".pdf":
                res = subprocess.run(
                    ["tesseract", str(tmp_path), "stdout"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.strip()

            return ""
        finally:
            tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _parse_docx(data: bytes) -> str:
        doc = docx.Document(io.BytesIO(data))
        blocks = []
        for para in doc.paragraphs:
            if para.text.strip():
                blocks.append(para.text.strip())

        # 将 Word 中的表格转为 Markdown 表格，确保公差与化学成分对应关系不丢失
        for table in doc.tables:
            md_rows = []
            for r_idx, row in enumerate(table.rows):
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                md_rows.append("| " + " | ".join(cells) + " |")
                if r_idx == 0:
                    md_rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
            if md_rows:
                blocks.append("\n".join(md_rows))

        return "\n\n".join(blocks)
