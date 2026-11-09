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
    def _clean_sparse_cjk_spaces(text: str) -> str:
        """
        消除因老旧 PDF 排版、扫描 OCR 或字距渲染导致的“中文字符间稀疏空格”。
        例如将：
        - "G  B  / T  3 1 0 3 . 3 -  2 0 0 0" -> "GB/T 3103.3-2000"
        - "前  台「  1" 或 "前  言" -> "前言"
        - "本 标 准 等 同 采 用 国 际 标 准" -> "本标准等同采用国际标准"
        同时保护正常的西文单词间空格（如 "ISO 4759-3" 不会被破坏）。
        并且针对标准文件中的“2 引用标准”章节、标准代号条目列表及跨行延续括号 (eqv ISO ...) 进行结构化修复与防表格保护。
        """
        # 0. 预处理 Unicode 制表框线与典型 OCR 符号噪点
        text = text.replace("│", "|").replace("┃", "|").replace("¦", "|").replace("丨", "|")
        text = re.sub(r"(?:(?<=[||\s])|^)(?:十|＋)(?=\s*[\d\{｛])", "+", text)
        text = re.sub(r"x\s*-\s*(?=\d)", "+", text)
        text = re.sub(r"\+\s*[\{｛]", "+1", text)
        text = re.sub(r"G\s*s\s*/\s*T", "GB/T", text, flags=re.IGNORECASE)
        # 规整行末粘连或带空格的单位（如 "m  m" -> "mm"）
        text = re.sub(r"m\s*m\s*$", " mm", text, flags=re.IGNORECASE | re.MULTILINE)

        # 修复通用表题格式（抽象支持：表 A1、表 A2、表 1、表 2 等任意表号，及 mm/℃/%/MPa 等通用工程单位）
        def fix_table_title(m: re.Match) -> str:
            raw_id = m.group(1).strip()
            raw_name = m.group(2).strip()
            unit = m.group(3) or ""
            t_id = re.sub(r"\s+", "", raw_id)
            lead_num_match = re.match(r"^(\d+)", raw_name)
            if lead_num_match and not re.search(r"\d", t_id):
                lead_num = lead_num_match.group(1)
                t_id += lead_num
                raw_name = raw_name[len(lead_num):].strip()
            t_name = re.sub(r"\s+", "", raw_name)
            unit_str = f"({unit.strip()})" if unit else ""
            return f"\n### 表 {t_id} {t_name}{unit_str}\n"

        text = re.sub(
            r"表\s*([A-Za-z0-9\-.]+)\s+([^\n|#]+?)(?:\s+([a-zA-Z%℃/]+(?:\s*[a-zA-Z%℃/]+)?))?$",
            fix_table_title,
            text,
            flags=re.MULTILINE,
        )

        lines = text.splitlines()

        # 0.1 先行解包误包为 Markdown 表格的标准引用条目（如 | GB/T | 1182-1996... | ISO | 1101... |）
        raw_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                parts = [p.strip() for p in stripped[1:-1].split("|")]
                if all(re.match(r"^:?-+:?$", p) for p in parts if p):
                    continue
                row_text = "  ".join([p for p in parts if p])
                # 若行内包含标准代号（GB/T, ISO 等）或 eqv/idt/neq 引用修饰
                if re.search(r"\b(?:GB(?:/[A-Z]+)?|ISO|IEC|DIN|ASTM|JIS|QC(?:/[A-Z]+)?)\b", row_text, re.IGNORECASE) or re.search(r"(?:eqv|idt|neq)", row_text, re.IGNORECASE):
                    raw_lines.append(row_text)
                    continue
            raw_lines.append(line)

        # 0.1 跨行延续括号自动向上缝合（例如换行缩进的 `(eqv ISO 286-1:1988)` 缝合到上一行标准末尾）
        stitched_lines = []
        for line in raw_lines:
            stripped = line.strip()
            if not stripped:
                stitched_lines.append("")
                continue
            is_dangling = bool(
                re.match(
                    r"^[（(]\s*(?:e\s*q\s*v|i\s*d\s*t|n\s*e\s*q|m\s*o\s*d|ref|ISO|GB|IEC)",
                    stripped,
                    re.IGNORECASE,
                )
            )
            if is_dangling and stitched_lines:
                idx = len(stitched_lines) - 1
                while idx >= 0 and not stitched_lines[idx]:
                    idx -= 1
                if idx >= 0:
                    stitched_lines[idx] = stitched_lines[idx] + " " + stripped
                    continue
            stitched_lines.append(line)

        cleaned_lines: List[str] = []
        cjk_range = r"\u4e00-\u9fa5\u3000-\u303f\uff00-\uffef"

        for line in stitched_lines:
            stripped = line.strip()
            if not stripped:
                cleaned_lines.append("")
                continue

            # 1. 检测章节标题规范化（如 2引用标准 / 2 引用标准 / 2. 规范性引用文件 -> ## 2 引用标准）
            if re.match(r"^2\s*[\.、]?\s*(?:引用标准|规范性引用文件)", stripped):
                norm_title = re.sub(r"^2\s*[\.、]?\s*", "## 2 ", stripped)
                cleaned_lines.append(norm_title)
                continue

            # 2. 检测是否属于典型的标准编号独立行（如 G B / T 3 1 0 3 . 3 - 2 0 0 0）
            if re.match(r"^[A-Za-z0-9\s/.\-_—–:：()（）]+$", stripped) and len(stripped) >= 5:
                compact_std = re.sub(r"(?<=[A-Za-z0-9/.\-_])\s+(?=[A-Za-z0-9/.\-_])", "", stripped)
                compact_std = re.sub(r"^([A-Za-z]+(?:/[A-Za-z]+)?)\s*([0-9])", r"\1 \2", compact_std)
                if any(k in compact_std.upper() for k in ("GB", "ISO", "DIN", "ASTM", "JIS", "QC")):
                    cleaned_lines.append(compact_std)
                    continue

            # 3. 对汉字密集或标准条目行进行清洗
            cjk_chars_count = len(re.findall(f"[{cjk_range}]", stripped))
            total_chars = len(stripped.replace(" ", ""))
            is_std_item = bool(
                re.match(
                    r"^(?:[-•*]\s*)?(?:GB(?:/[A-Za-z0-9]+)?|ISO|IEC|DIN|ASTM|JIS|QC(?:/[A-Za-z0-9]+)?|JB(?:/[A-Za-z0-9]+)?|HB|SAE|EN)\b",
                    stripped,
                    re.IGNORECASE,
                )
            )

            # 若该行主要由中文组成（中文字符占比 >= 30%）或属于标准条目引用行，执行平滑去空
            if (total_chars > 0 and (cjk_chars_count / total_chars) >= 0.3) or is_std_item:
                cur = stripped
                # 融合两个中文之间的 1~3 个空格：如 "标 准" -> "标准"
                cur = re.sub(f"([{cjk_range}])\\s+([{cjk_range}])", r"\1\2", cur)
                # 多次执行直到收敛（处理连续单字带空格场景）
                for _ in range(3):
                    next_cur = re.sub(f"([{cjk_range}])\\s+([{cjk_range}])", r"\1\2", cur)
                    if next_cur == cur:
                        break
                    cur = next_cur

                # 融合中文字符与常见中文标点之间的空格：如 "条 ）" -> "条）"
                cur = re.sub(f"([{cjk_range}])\\s+([，。、；：！？（）()《》“”‘’——])", r"\1\2", cur)
                cur = re.sub(f"([，。、；：！？（）()《》“”‘’——])\\s+([{cjk_range}])", r"\1\2", cur)

                # 消除数字及大写英文字母内部被误拆开的细微单空格（如 3 1 0 3 -> 3103, IS O -> ISO）
                for _ in range(4):
                    cur = re.sub(r"(?<=\d)\s+(?=\d)", "", cur)
                for _ in range(3):
                    cur = re.sub(r"(?<=[A-Z])\s+(?=[A-Z])", "", cur)

                # 规范标准号/连字符/斜杠内部空格（如 GB / T -> GB/T, 4759 - 3 -> 4759-3）
                cur = re.sub(r"([A-Za-z0-9])\s*([/\-_—–])\s*([A-Za-z0-9])", r"\1\2\3", cur)

                # 修复字母散列缩写（如 e q v -> eqv, i d t -> idt, n e q -> neq）
                cur = re.sub(r"\b([eEiInNmM])\s+([qQdDeEoO])\s+([vVtTqQdD])\b", r"\1\2\3", cur)
                # 规整等效采用短语与后接标准（如 eqvISO -> eqv ISO, idtGB -> idt GB）
                cur = re.sub(r"(eqv|idt|neq|mod)\s*([A-Za-z])", r"\1 \2", cur, flags=re.IGNORECASE)
                cur = re.sub(r"\b(ISO|GB|IEC|DIN|ASTM|JIS)(\d+)", r"\1 \2", cur)

                # 多级条款号点号及冒号规整（如 3 . 5 . 1 -> 3.5.1, : 2000 -> :2000）
                cur = re.sub(r"(\d+)\s*\.", r"\1.", cur)
                cur = re.sub(r"\.\s*(\d+)", r".\1", cur)
                cur = re.sub(r"\s*([:：])\s*(\d+)", r":\2", cur)

                # 处理括号格式及去重（如 (( -> (, ）） -> ））
                cur = re.sub(r"([（(])\s*", r"\1", cur)
                cur = re.sub(r"\s*([）)])", r"\1", cur)
                cur = re.sub(r"[（(]{2,}", "(", cur)
                cur = re.sub(r"[）)]{2,}", ")", cur)
                cur = re.sub(r"^([a-zA-Z0-9][)）]?)\s+", r"\1", cur)

                # 针对标准引用项，保证发布年份后与中文标准名称之间有清晰空格（如 GB/T 1182-1996 形状和位置公差）
                cur = re.sub(f"(\\b\\d{{4}})([{cjk_range}])", r"\1 \2", cur)
                cur = re.sub(r"\s*[（(]\s*(eqv|idt|neq)", r" (\1", cur, flags=re.IGNORECASE)

                # 融合孤立的单字母/数字与中文之间的空格（如 "h 的 尺 寸" -> "h的尺寸"；"c 级" -> "c级"；"3.1 条" -> "3.1条"）
                # 注意：通过 (?![a-zA-Z0-9]) 保护多字母西文单词（如 "ISO 4759"）与中文之间的自然空格
                cur = re.sub(f"([{cjk_range}])\\s+([a-zA-Z0-9])(?![a-zA-Z0-9])", r"\1\2", cur)
                cur = re.sub(f"(?<![a-zA-Z0-9])([a-zA-Z0-9])\\s+([{cjk_range}])", r"\1\2", cur)
                cur = re.sub(f"(\\b\\d{{1,2}}(?:\\.\\d+)*)\\s+([{cjk_range}])", r"\1\2", cur)
                cur = re.sub(f"([{cjk_range}])\\s+(\\b\\d{{1,2}}(?:\\.\\d+)*)", r"\1\2", cur)

                # 若属于标准条目，消除连续多余空格并规范为 Markdown 列表项 `- `
                if is_std_item:
                    cur = re.sub(r"\s{2,}", " ", cur)
                    cur = re.sub(r"^[-•*]?\s*", "- ", cur)

                cleaned_lines.append(cur)
            else:
                cleaned_lines.append(stripped)

        return "\n".join(cleaned_lines)

    @staticmethod
    def _format_space_aligned_tables(text: str) -> str:
        """
        将 PDF 布局模式提取出的多空格对齐表格行自动识别并转换为 Markdown 表格 (`| col1 | col2 |`)，
        确保公差矩阵表与化学成分表的行/列对应关系在送入 LLM 时不丢失。
        同时内置防假表格守门机制：列数过多但每列基本都是单字的普通段落或标准引用条目严禁当做表格拆分！
        """
        # 先对文本做中文字距稀疏空格清洗与标准引用行规范化
        normalized_text = DocumentParser._clean_sparse_cjk_spaces(text)

        lines = normalized_text.splitlines()
        output_lines: List[str] = []
        table_buffer: List[List[str]] = []

        def clean_table_cell_display(c: str) -> str:
            # 消除单元格内部纯数字/符号的碎空格，保留尺寸范围或多单词的正常间距
            norm = re.sub(r"\s+", "", c)
            norm = norm.replace("＋", "+").replace("－", "-").replace("．", ".").replace("，", ",")
            norm = re.sub(r"^[一—–－]\s*", "-", norm)
            if norm in ("O", "o", "。"):
                norm = "0"
            if re.match(r"^[+-]?[0-9]+(?:\.[0-9]+)?$", norm) or norm in ("0", "+0", "-0"):
                return norm
            return re.sub(r"\s+", " ", c).strip()

        def flush_table() -> None:
            nonlocal table_buffer
            if len(table_buffer) >= 2:
                max_cols = max(len(r) for r in table_buffer)
                for r_idx, row in enumerate(table_buffer):
                    cleaned_row = [clean_table_cell_display(c) for c in row]
                    padded = cleaned_row + [""] * (max_cols - len(cleaned_row))
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

            # 若行首是标准条目或属于标准引用，绝对严禁作为表格行！
            is_std_ref_line = bool(
                re.match(r"^(?:[-•*]\s*)?(?:GB(?:/[A-Za-z0-9]+)?|ISO|IEC|DIN|ASTM|JIS|QC(?:/[A-Za-z0-9]+)?|JB(?:/[A-Za-z0-9]+)?|HB|SAE|EN)\b", stripped, re.IGNORECASE)
                or re.search(r"\b(?:eqv|idt|neq|MOD)\s+(?:ISO|IEC|GB|EN)\b", stripped, re.IGNORECASE)
            )
            if is_std_ref_line:
                if table_buffer:
                    flush_table()
                output_lines.append(line)
                continue

            # 识别以 2 个及以上连续空格或制表符分隔的多列数据行
            cols = [c.strip() for c in re.split(r"\t+|\s{2,}", stripped) if c.strip()]

            # 防假表格守门逻辑（Anti-False-Table Guard）：
            # 1. 若列数很多（例如 >= 8 列），但平均列长 <= 2 个字符，这必然是散开的中文字段落，绝不是表格！
            # 2. 行首带条款号编号（如 3.1、a)、b)）的行通常是条款正文或列表项，不作为表格行
            avg_col_len = sum(len(c) for c in cols) / max(1, len(cols))
            is_fragmented_chinese = len(cols) >= 6 and avg_col_len <= 2.2

            is_table_row = (
                not is_fragmented_chinese
                and not re.match(r"^(?:\d+\.\d+|[a-zA-Z][)）]|[-•*])\s*", stripped)
                and (
                    len(cols) >= 3
                    or (len(cols) == 2 and bool(re.search(r"[0-9±≤≥~～%Øφ]", stripped)))
                )
            )

            if is_table_row:
                table_buffer.append(cols)
            else:
                if table_buffer:
                    flush_table()
                output_lines.append(line)

        if table_buffer:
            flush_table()

        # 对制造标准中常见的上下偏差叠放矩阵行进行自动折叠与格式化
        folded_lines = DocumentParser._fold_stacked_tolerance_rows(output_lines)
        return "\n".join(folded_lines)

    @staticmethod
    def _fold_stacked_tolerance_rows(lines: List[str]) -> List[str]:
        """
        折叠制造标准中因上下偏差纵向排版而分裂为相邻两行的公差矩阵行。
        例如将：
        | | 3 | +0.14 | +0.25 | +0.4 |
        | | | 0 | 0 | 0 |
        自动配对融合成单一的高质量 Markdown 表格行：
        | ≤ 3 | +0.14 / 0 | +0.25 / 0 | +0.4 / 0 |
        同时压缩所有表格单元格中的 OCR 碎空格与错符，极大精简 Token 占用。
        """
        def normalize_cell(c: str) -> str:
            s = re.sub(r"\s+", "", c)
            s = s.replace("＋", "+").replace("－", "-").replace("．", ".").replace("，", ",")
            s = re.sub(r"^[一—–－]\s*", "-", s)
            if s in ("O", "o", "。"):
                s = "0"
            return s

        def clean_display_cell(c: str) -> str:
            norm = normalize_cell(c)
            if re.match(r"^[+-]?[0-9]+(?:\.[0-9]+)?$", norm) or norm in ("0", "+0", "-0"):
                return norm
            return re.sub(r"\s+", " ", c).strip()

        output_lines: List[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                raw_cols = stripped[1:-1].split("|")
                cols = [normalize_cell(c) for c in raw_cols]

                # 3.1 识别多层复合表头中的总括列头（如 | 公称直径 | 极限偏差 | 或 | 公称厚度 | 极限偏差 |），跳过
                if len(cols) <= 3 and any(k in "".join(cols) for k in ("公称", "基本尺寸", "尺寸", "极限偏差", "公差", "成分", "项目")):
                    i += 1
                    continue

                # 3.2 识别等级与属性子表头（抽象支持 H13、h11、IT10、A级、C级、精密级等任意工程等级）
                grade_cols = [c for c in cols if re.match(r"^(?:[A-Za-z]+\d*|\d+级|[A-Za-z]级|[A-Z][a-z]?|精密.*|中等.*|粗糙.*)$", c)]
                if len(grade_cols) >= 2:
                    headers = ["基本尺寸分段 (mm)"] + grade_cols
                    output_lines.append("| " + " | ".join(headers) + " |")
                    output_lines.append("| " + " | ".join([":---"] * len(headers)) + " |")
                    i += 1
                    continue

                # 3.3 探测下一行是否为通用的“下偏差行”（拓扑特征判定：前置尺寸列为空，后随纯偏差数字，无汉字正文）
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                next_stripped = lines[j].strip() if j < len(lines) else ""
                if next_stripped.startswith("|") and next_stripped.endswith("|"):
                    next_raw_cols = next_stripped[1:-1].split("|")
                    next_cols = [normalize_cell(c) for c in next_raw_cols]
                    non_empty = [c for c in next_cols if c]
                    is_stacked_lower = (
                        non_empty
                        and (not next_cols[0] or next_cols[0] in ("-", "—", ""))
                        and all(c in ("0", "+0", "-0") or re.match(r"^[+-]?[0-9.]+$", c) for c in non_empty)
                        and len(non_empty) <= max(1, len(cols) - 1)
                        and not any(re.search(r"[\u4e00-\u9fa5]", c) for c in non_empty)
                    )
                    if is_stacked_lower:
                        d_min = cols[0] if len(cols) > 0 else ""
                        d_max = cols[1] if len(cols) > 1 else ""
                        if not d_min or d_min in ("—", "-", ""):
                            range_str = f"≤ {d_max}"
                        else:
                            range_str = f">{d_min} ~ {d_max}"

                        upper_vals = cols[2:] if len(cols) > 2 else []
                        lower_vals = [c for c in next_cols if c]

                        # 扩展因排版粘连的公差（如 +0.3+0.48）
                        expanded_upper = []
                        for u in upper_vals:
                            sub_vals = re.findall(r"[+-]?[0-9.]+", u)
                            if len(sub_vals) > 1:
                                expanded_upper.extend(sub_vals)
                            elif u:
                                expanded_upper.append(u)

                        merged = []
                        for u_idx, u in enumerate(expanded_upper):
                            l = lower_vals[u_idx] if u_idx < len(lower_vals) else "0"
                            merged.append(f"{u} / {l}")

                        output_lines.append("| " + " | ".join([range_str] + merged) + " |")
                        i = j + 1
                        continue

                # 普通表格行：压紧每个单元格内部多余碎空格
                cleaned_cols = [clean_display_cell(c) for c in raw_cols]
                output_lines.append("| " + " | ".join(cleaned_cols) + " |")
                i += 1
                continue

            output_lines.append(line)
            i += 1

        return output_lines

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
