"""
文档解析模块
支持将上传的 Markdown (.md)、纯文本 (.txt)、PDF (.pdf)、Word (.docx) 转换为结构化文本（保留表格结构）。
"""

import io
from pathlib import Path
from pypdf import PdfReader
import docx


class DocumentParser:
    """多格式标准文档解析器"""

    @staticmethod
    def parse_bytes(filename: str, data: bytes) -> str:
        ext = Path(filename).suffix.lower()
        if ext in (".md", ".txt", ".markdown"):
            return data.decode("utf-8", errors="replace")
        elif ext == ".pdf":
            return DocumentParser._parse_pdf(data)
        elif ext == ".docx":
            return DocumentParser._parse_docx(data)
        else:
            raise ValueError(f"暂不支持的文件格式: {ext}（请上传 .md, .txt, .pdf 或 .docx）")

    @staticmethod
    def _parse_pdf(data: bytes) -> str:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for idx, page in enumerate(reader.pages, start=1):
            txt = page.extract_text() or ""
            if txt.strip():
                pages.append(f"<!-- 第 {idx} 页 -->\n{txt.strip()}")
        return "\n\n".join(pages)

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
