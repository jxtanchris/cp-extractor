"""
FastAPI 可视化工作台与 RESTful 接口服务
提供：
- 标准文档上传解析 (.md / .txt / .pdf / .docx)
- 四类基准样本一键加载
- 提示词组装实时预览
- DeepSeek 大模型 / 内置规则双引擎提取
- 质量门禁人工复核与放行
- 控制计划 Excel / JSON 一键下载
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from ..core.classifier import StandardClassifier
from ..core.extractor import StandardExtractorPipeline
from ..core.llm_client import DeepSeekClient, JSON_SCHEMA_INSTRUCTION
from ..core.mapper import ControlPlanExcelMapper
from ..core.parser import DocumentParser
from ..core.prompt_builder import PromptBuilder
from ..schemas.models import CheckItem, ExtractionPackage

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
SAMPLES_DIR = ROOT_DIR / "tests" / "benchmark_samples"
OUTPUT_DIR = ROOT_DIR / "output"
STATIC_DIR = Path(__file__).resolve().parent / "static"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="CP-Extractor 标准→控制计划检查项提取系统",
    description="基于 RAG/LLM (DeepSeek) + 类别专用插件 + 双轨门禁校验的汽车质量控制计划提取平台",
    version="1.0.0",
)


class PreviewPromptRequest(BaseModel):
    content: str
    manual_category: Optional[str] = None


class ExtractRequest(BaseModel):
    file_name: str = Field(default="custom_standard.md")
    content: str
    manual_category: Optional[str] = None
    engine_mode: str = Field(default="auto", description="deepseek | rule | auto")
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    deepseek_model: Optional[str] = None


class ReviewUpdateRequest(BaseModel):
    package: Dict[str, Any]
    review_index: int
    action: str = Field(..., description="approve | reject")
    updated_item: Optional[Dict[str, Any]] = None


class TemplateUpdateRequest(BaseModel):
    content: str = Field(..., description="更新后的模板 Markdown 内容")


SAMPLE_META = {
    "sample_tolerance_iso2768.md": {
        "title": "公差/尺寸标准样本 (ISO 2768 / GD&T)",
        "badge": "公差/尺寸",
    },
    "sample_material_qste420tm.md": {
        "title": "材料标准样本 (大梁钢 QStE420TM)",
        "badge": "材料标准",
    },
    "sample_process_carburizing.md": {
        "title": "工艺标准样本 (齿轮渗碳淬火热处理)",
        "badge": "工艺标准",
    },
    "sample_appearance_coating.md": {
        "title": "外观/性能标准样本 (外饰电泳与面漆)",
        "badge": "外观/性能",
    },
}


def _safe_stem(filename: str) -> str:
    stem = Path(filename).stem
    return re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fa5]", "_", stem) or "standard"


def _persist_uploaded_source(filename: str, raw: bytes) -> str:
    """将用户上传的原始标准文档（如 PDF）持久化到 output/ 目录，返回可直接在浏览器打开并定位页码的 URL"""
    ext = Path(filename).suffix.lower() or ".pdf"
    safe_name = f"{_safe_stem(filename)}_source{ext}"
    source_path = OUTPUT_DIR / safe_name
    source_path.write_bytes(raw)
    return f"/api/source/{safe_name}"


def _persist_artifacts(package: ExtractionPackage) -> Dict[str, str]:
    stem = _safe_stem(package.standard_info.file_name)
    json_name = f"{stem}_output.json"
    excel_name = f"{stem}_control_plan.xlsx"

    json_path = OUTPUT_DIR / json_name
    excel_path = OUTPUT_DIR / excel_name

    json_path.write_text(
        json.dumps(package.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    mapper = ControlPlanExcelMapper()
    mapper.export(package, excel_path)

    return {
        "json_download_url": f"/api/download/{json_name}",
        "excel_download_url": f"/api/download/{excel_name}",
    }


@app.get("/", response_class=HTMLResponse)
def index():
    html_file = STATIC_DIR / "index.html"
    if not html_file.exists():
        raise HTTPException(status_code=404, detail="前端界面文件未找到")
    return HTMLResponse(
        content=html_file.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/config")
def get_config():
    from dotenv import load_dotenv
    load_dotenv(override=True)
    env_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    return {
        "has_env_key": bool(env_key),
        "masked_env_key": f"{env_key[:6]}...{env_key[-4:]}" if len(env_key) > 10 else "",
        "default_base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "default_model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    }


@app.get("/api/samples")
def list_samples() -> List[Dict[str, str]]:
    result = []
    for fname, meta in SAMPLE_META.items():
        fpath = SAMPLES_DIR / fname
        if fpath.exists():
            result.append({
                "file_name": fname,
                "title": meta["title"],
                "badge": meta["badge"],
            })
    return result


@app.get("/api/samples/{sample_name}")
def get_sample(sample_name: str):
    fpath = SAMPLES_DIR / sample_name
    if not fpath.exists() or fpath.parent != SAMPLES_DIR:
        raise HTTPException(status_code=404, detail=f"样本不存在: {sample_name}")
    content = fpath.read_text(encoding="utf-8")
    classifier = StandardClassifier()
    key, cat_enum = classifier.classify(content)
    return {
        "file_name": sample_name,
        "content": content,
        "detected_key": key,
        "detected_category": cat_enum.value,
    }


@app.post("/api/upload")
async def upload_standard(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        content = DocumentParser.parse_bytes(file.filename or "uploaded.txt", raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    source_file_url = _persist_uploaded_source(file.filename or "uploaded.txt", raw)
    classifier = StandardClassifier()
    key, cat_enum = classifier.classify(content)
    return {
        "file_name": file.filename,
        "content": content,
        "detected_key": key,
        "detected_category": cat_enum.value,
        "source_file_url": source_file_url,
    }


@app.post("/api/extract-document")
async def extract_from_uploaded_document(
    file: UploadFile = File(...),
    category: str = Form(default="tolerance"),
    deepseek_api_key: Optional[str] = Form(default=None),
    deepseek_base_url: Optional[str] = Form(default=None),
    deepseek_model: Optional[str] = Form(default=None),
):
    """
    核心三步闭环接口：
    1. 接收上传的 PDF / Word / Markdown 标准文件并解析正文
    2. 根据所选标准类别组装 (公共基础模板 + 类别专用插件)
    3. 调用 DeepSeek LLM 提取检查项 -> 后处理门禁校验 -> 生成 Excel 与 JSON
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传的文件为空")

    try:
        parsed_text = DocumentParser.parse_bytes(file.filename or "standard.pdf", raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not parsed_text.strip():
        raise HTTPException(
            status_code=400,
            detail="无法从该文件中提取出有效文本（如果是扫描件图片型 PDF，请先经 OCR/MinerU 转换）",
        )

    source_file_url = _persist_uploaded_source(file.filename or "standard.pdf", raw)

    ds_client = DeepSeekClient(
        api_key=deepseek_api_key,
        base_url=deepseek_base_url,
        model=deepseek_model,
    )
    if not ds_client.is_configured:
        raise HTTPException(
            status_code=400,
            detail="未检测到 DeepSeek API Key。请点击右上角【DeepSeek API 配置】填入你的 Key，或在 .env 文件中配置 DEEPSEEK_API_KEY。",
        )

    try:
        pipeline = StandardExtractorPipeline(llm_callable=ds_client.extract_check_items_json)
        package = pipeline.run(
            file_name=file.filename or "standard.pdf",
            content=parsed_text,
            manual_category=category if category != "auto" else None,
        )
        package.standard_info.source_file_url = source_file_url
        downloads = _persist_artifacts(package)
        return {
            "engine_used": f"DeepSeek ({ds_client.model})",
            "parsed_length": len(parsed_text),
            "parsed_preview": parsed_text[:2000],
            "source_file_url": source_file_url,
            "package": package.model_dump(mode="json"),
            **downloads,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/templates")
def list_templates():
    """获取所有提示词模板（公共基础模板 + 四类专用插件）"""
    builder = PromptBuilder()
    return {
        "templates": builder.list_all_templates(),
        "json_schema_instruction": JSON_SCHEMA_INSTRUCTION.strip(),
    }


@app.put("/api/templates/{template_key}")
def update_template(template_key: str, req: TemplateUpdateRequest):
    """保存修改后的提示词模板内容"""
    builder = PromptBuilder()
    try:
        updated = builder.save_template(template_key, req.content)
        return updated
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存模板失败: {e}")


@app.post("/api/templates/{template_key}/reset")
def reset_template(template_key: str):
    """将指定提示词模板恢复为系统默认内容"""
    builder = PromptBuilder()
    try:
        reset_data = builder.reset_template(template_key)
        return reset_data
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"恢复默认模板失败: {e}")


@app.post("/api/preview-prompt")
def preview_prompt(req: PreviewPromptRequest):
    classifier = StandardClassifier()
    builder = PromptBuilder()
    try:
        key, cat_enum = classifier.classify(req.content, manual_category=req.manual_category)
        meta, _ = builder.get_plugin(key)
        prompt = builder.build_extraction_prompt(key, context_text=req.content)
        full_prompt = f"{prompt}\n\n{JSON_SCHEMA_INSTRUCTION}"
        return {
            "plugin_key": key,
            "standard_category": cat_enum.value,
            "template_version": f"v{meta.get('version', '1.0')}",
            "prompt": full_prompt,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/extract")
def run_extraction(req: ExtractRequest):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="标准文本内容不能为空")

    ds_client = DeepSeekClient(
        api_key=req.deepseek_api_key,
        base_url=req.deepseek_base_url,
        model=req.deepseek_model,
    )

    use_deepseek = False
    if req.engine_mode == "deepseek":
        if not ds_client.is_configured:
            raise HTTPException(
                status_code=400,
                detail="已选择 DeepSeek 模式，但未检测到 API Key。请在右上角【DeepSeek 配置】中填入 API Key 或在 .env 中设置 DEEPSEEK_API_KEY。",
            )
        use_deepseek = True
    elif req.engine_mode == "auto" and ds_client.is_configured:
        use_deepseek = True

    try:
        if use_deepseek:
            pipeline = StandardExtractorPipeline(llm_callable=ds_client.extract_check_items_json)
            engine_label = f"DeepSeek API ({ds_client.model})"
        else:
            pipeline = StandardExtractorPipeline(llm_callable=None)
            engine_label = "内置结构化基准引擎 (离线免Key)"

        package = pipeline.run(
            file_name=req.file_name,
            content=req.content,
            manual_category=req.manual_category or None,
        )
        downloads = _persist_artifacts(package)
        return {
            "engine_used": engine_label,
            "package": package.model_dump(mode="json"),
            **downloads,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/approve-review")
def approve_or_reject_review_item(req: ReviewUpdateRequest):
    """人工审核门禁交互：质量工程师修改并准入待确认项，或驳回剔除，并同步刷新 Excel"""
    try:
        package = ExtractionPackage.model_validate(req.package)
        if req.review_index < 0 or req.review_index >= len(package.pending_review_items):
            raise HTTPException(status_code=400, detail="待审核项索引越界")

        target_item = package.pending_review_items.pop(req.review_index)

        if req.action == "approve":
            if req.updated_item:
                target_item = CheckItem.model_validate(req.updated_item)
            target_item.needs_human_review = False
            package.check_items.append(target_item)

        package.refresh_summary()
        downloads = _persist_artifacts(package)
        return {
            "package": package.model_dump(mode="json"),
            **downloads,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"审核操作失败: {e}")


@app.get("/api/source/{filename}")
def view_source_file(filename: str):
    """在线预览上传的源标准文档（如 PDF），使用 inline 模式以支持浏览器直接定位 #page=N 页码"""
    safe_name = Path(filename).name
    target = OUTPUT_DIR / safe_name
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"源文件不存在: {safe_name}")
    ext = target.suffix.lower()
    media_type = "application/pdf" if ext == ".pdf" else None
    return FileResponse(
        path=target,
        media_type=media_type,
        content_disposition_type="inline",
    )


@app.get("/api/download/{filename}")
def download_artifact(filename: str):
    safe_name = Path(filename).name
    target = OUTPUT_DIR / safe_name
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {safe_name}")
    return FileResponse(path=target, filename=safe_name)
