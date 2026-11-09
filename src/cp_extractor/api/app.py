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
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from ..core.auth import AuthManager
from ..core.classifier import StandardClassifier
from ..core.extractor import StandardExtractorPipeline
from ..core.feedback_learner import FeedbackRuleLearner
from ..core.lexicon_db import LexiconDB
from ..core.llm_client import (
    CHAR_LIST_JSON_INSTRUCTION,
    DeepSeekClient,
    JSON_SCHEMA_INSTRUCTION,
    LLMClient,
    LLM_PROVIDERS,
    get_providers_status,
    persist_llm_config_to_env,
)
from ..core.mapper import CharacteristicListExcelMapper, ControlPlanExcelMapper
from ..core.parser import DocumentParser
from ..core.prompt_builder import PromptBuilder
from ..schemas.auth import (
    TokenResponse,
    UserLoginRequest,
    UserRegisterRequest,
    UserResponse,
)
from ..schemas.models import (
    CharacteristicEntry,
    CharacteristicListPackage,
    CheckItem,
    ExtractionPackage,
)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
SAMPLES_DIR = ROOT_DIR / "tests" / "benchmark_samples"
OUTPUT_DIR = ROOT_DIR / "output"
STATIC_DIR = Path(__file__).resolve().parent / "static"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

security_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
) -> UserResponse:
    """FastAPI 权限拦截依赖项：校验 Bearer Token 并注入当前用户，未登录抛出 401"""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="必须登录后才能使用上传与提取功能",
            headers={"WWW-Authenticate": "Bearer"},
        )
    auth_mgr = AuthManager()
    try:
        return auth_mgr.authenticate_token(credentials.credentials)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )

app = FastAPI(
    title="CP-Extractor 标准→控制计划检查项提取系统",
    description="基于 RAG/LLM (DeepSeek) + 类别专用插件 + 双轨门禁校验的汽车质量控制计划提取平台",
    version="1.0.0",
)


class PreviewPromptRequest(BaseModel):
    content: str
    manual_category: Optional[str] = None
    mode: str = Field(default="full", description="full | char_list")


class ExtractRequest(BaseModel):
    file_name: str = Field(default="custom_standard.md")
    content: str
    manual_category: Optional[str] = None
    mode: str = Field(default="full", description="full | char_list")
    engine_mode: str = Field(default="auto", description="llm | deepseek | rule | auto")
    llm_provider: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_temperature: Optional[float] = None
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    deepseek_model: Optional[str] = None


class LLMConfigActionRequest(BaseModel):
    provider: str = Field(default="deepseek", description="大模型服务商标识: deepseek | qwen | zhipu | kimi | siliconflow | openai | ollama | custom")
    api_key: Optional[str] = Field(default=None, description="API Key（若留空则尝试使用服务端 .env 已配置的 Key）")
    base_url: str = Field(..., description="OpenAI 兼容接口 Base URL")
    model: str = Field(..., description="模型名称")
    temperature: Optional[float] = Field(default=0.0, ge=0.0, le=2.0, description="采样温度 (0.0 ~ 2.0)")


class ReviewUpdateRequest(BaseModel):
    package: Dict[str, Any]
    review_index: int
    action: str = Field(..., description="approve | reject")
    updated_item: Optional[Dict[str, Any]] = None


class TemplateUpdateRequest(BaseModel):
    content: str = Field(..., description="更新后的模板 Markdown 内容")


class LexiconCreateRequest(BaseModel):
    category: str = Field(..., description="词库分类: method | unit | criteria")
    keyword: str = Field(..., description="要添加的词条（支持逗号或换行分隔批量输入）")
    lang: str = Field(default="zh", description="主词条语种: zh | en | ja | de | all")
    aliases: str = Field(default="", description="多语言同义词/外文对照（逗号分隔，如 CMM, 三次元測定機）")
    description: str = Field(default="", description="词条说明备注（可选）")


class LexiconUpdateRequest(BaseModel):
    keyword: str = Field(..., description="修改后的词条名称")
    lang: Optional[str] = Field(default=None, description="修改后的主词条语种: zh | en | ja | de | all")
    aliases: Optional[str] = Field(default=None, description="修改后的多语言同义词/外文对照")
    description: str = Field(default="", description="修改后的词条说明备注")


class LexiconResetRequest(BaseModel):
    category: Optional[str] = Field(default="all", description="要重置的分类: method | unit | criteria | all")


class LearningSettingsRequest(BaseModel):
    threshold: int = Field(..., ge=1, le=20, description="自动升格为正式规则所需的手工确认/驳回累计次数阈值")


class LearnedRuleUpdateRequest(BaseModel):
    action: Optional[str] = Field(default=None, description="approve | reject")
    is_active: Optional[bool] = Field(default=None, description="是否立即启用该规则")
    learned_special_char_class: Optional[str] = Field(default=None, description="自动套用的特殊特性分类")
    learned_specification: Optional[str] = Field(default=None, description="自动套用的规范/公差")
    learned_method: Optional[str] = Field(default=None, description="自动套用的检查方法")
    learned_criteria: Optional[str] = Field(default=None, description="自动套用的判定准则")
    learned_sample_frequency: Optional[str] = Field(default=None, description="自动套用的样本容量/频次")
    learned_control_method: Optional[str] = Field(default=None, description="自动套用的控制方法")
    learned_reaction_plan: Optional[str] = Field(default=None, description="自动套用的反应计划")


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


def _persist_artifacts(package: Union[ExtractionPackage, CharacteristicListPackage]) -> Dict[str, str]:
    stem = _safe_stem(package.standard_info.file_name)
    is_char_list = isinstance(package, CharacteristicListPackage)
    json_name = f"{stem}_char_list.json" if is_char_list else f"{stem}_output.json"
    excel_name = f"{stem}_characteristics.xlsx" if is_char_list else f"{stem}_control_plan.xlsx"

    json_path = OUTPUT_DIR / json_name
    excel_path = OUTPUT_DIR / excel_name

    json_path.write_text(
        json.dumps(package.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if is_char_list:
        mapper = CharacteristicListExcelMapper()
    else:
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


# ---------------------------------------------------------
# 用户认证与账号管理接口 (Phase 1)
# ---------------------------------------------------------

@app.post("/api/auth/register", response_model=TokenResponse)
def register_user(req: UserRegisterRequest):
    """新用户注册，成功后自动签发登录 Token"""
    auth_mgr = AuthManager()
    try:
        user = auth_mgr.register_user(
            username=req.username,
            password=req.password,
            display_name=req.display_name or "",
        )
        token = auth_mgr.issue_token_for_user(user)
        return TokenResponse(access_token=token, token_type="bearer", user=user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/auth/login", response_model=TokenResponse)
def login_user(req: UserLoginRequest):
    """用户登录接口，校验账号密码并返回 Bearer Token"""
    auth_mgr = AuthManager()
    try:
        user = auth_mgr.authenticate_user(username=req.username, password=req.password)
        token = auth_mgr.issue_token_for_user(user)
        return TokenResponse(access_token=token, token_type="bearer", user=user)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/auth/me", response_model=UserResponse)
def get_current_user_info(current_user: UserResponse = Depends(get_current_user)):
    """获取当前登录用户的身份信息"""
    return current_user


@app.post("/api/auth/logout")
def logout_user(current_user: UserResponse = Depends(get_current_user)):
    """安全退出登录"""
    return {"logged_out": True, "message": f"用户 {current_user.username} 已安全退出登录"}


@app.get("/api/config")
def get_config():
    return get_providers_status()


@app.get("/api/llm/providers")
def list_llm_providers():
    """获取所有内置支持的大模型厂商预设列表与当前环境变量状态"""
    return get_providers_status()


@app.post("/api/llm/test")
def test_llm_connection(req: LLMConfigActionRequest):
    """在线测试所选大模型厂商、Base URL、Model、Temperature 与 API Key 的连通性"""
    client = LLMClient(
        provider=req.provider,
        api_key=req.api_key,
        base_url=req.base_url,
        model=req.model,
        temperature=req.temperature,
    )
    try:
        return client.test_connection()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/llm/save-env")
def save_llm_config_to_env(req: LLMConfigActionRequest):
    """将当前大模型厂商配置持久化写入服务端 .env 文件"""
    env_path = ROOT_DIR / ".env"
    try:
        persist_llm_config_to_env(
            env_path=env_path,
            provider=req.provider,
            api_key=req.api_key,
            base_url=req.base_url,
            model=req.model,
            temperature=req.temperature,
        )
        return {
            "saved": True,
            "config": get_providers_status(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入 .env 失败: {e}")


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


def _build_chunks_routing_info(content: str, manual_category: Optional[str] = None) -> List[Dict[str, Any]]:
    """生成文档语义分块及每块动态匹配的插件路由摘要，供前端『查看 PDF 解析原文本』弹窗展示"""
    classifier = StandardClassifier()
    primary_key, _ = classifier.classify(content, manual_category=manual_category)
    chunks = StandardExtractorPipeline.split_document_into_chunks(content)
    info_list: List[Dict[str, Any]] = []
    for idx, ch in enumerate(chunks, start=1):
        plugins = classifier.classify_chunk_plugins(
            ch, default_plugin_key=primary_key, manual_category=manual_category
        )
        info_list.append({
            "chunk_index": idx,
            "char_length": len(ch),
            "matched_plugins": plugins,
        })
    return info_list


@app.post("/api/upload")
async def upload_standard(
    file: UploadFile = File(...),
    current_user: UserResponse = Depends(get_current_user),
):
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
        "parsed_length": len(content),
        "detected_key": key,
        "detected_category": cat_enum.value,
        "source_file_url": source_file_url,
        "chunks_info": _build_chunks_routing_info(content),
    }


@app.post("/api/extract-document")
async def extract_from_uploaded_document(
    file: UploadFile = File(...),
    category: str = Form(default="tolerance"),
    mode: str = Form(default="full"),
    llm_provider: Optional[str] = Form(default=None),
    llm_api_key: Optional[str] = Form(default=None),
    llm_base_url: Optional[str] = Form(default=None),
    llm_model: Optional[str] = Form(default=None),
    llm_temperature: Optional[float] = Form(default=None),
    deepseek_api_key: Optional[str] = Form(default=None),
    deepseek_base_url: Optional[str] = Form(default=None),
    deepseek_model: Optional[str] = Form(default=None),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    核心三步闭环接口：
    1. 接收上传的 PDF / Word / Markdown / 截图标准文件并解析正文
    2. 根据所选标准类别组装 (公共基础模板 + 类别专用插件)
    3. 调用所选后端大模型 (DeepSeek / Qwen / GLM / Kimi / OpenAI / Ollama 等) 提取检查项 -> 后处理门禁校验 -> 生成 Excel 与 JSON
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

    # 严格使用服务端环境变量配置的大模型 API Key，禁止用户各自配置个人 Key
    llm_client = LLMClient(
        provider=llm_provider,
        api_key=None,
        base_url=llm_base_url or deepseek_base_url,
        model=llm_model or deepseek_model,
        temperature=llm_temperature,
    )
    if not llm_client.is_configured:
        raise HTTPException(
            status_code=400,
            detail=f"未检测到服务端 {llm_client.provider_name} API Key 配置。请管理员在服务端 .env 文件中配置对应 API Key。",
        )

    try:
        manual_cat = category if category != "auto" else None
        if mode == "char_list":
            pipeline = StandardExtractorPipeline(llm_callable=llm_client.extract_characteristic_list_json)
            package = pipeline.run_characteristic_list(
                file_name=file.filename or "standard.pdf",
                content=parsed_text,
                manual_category=manual_cat,
                refiner=llm_client.refine_characteristics_with_feedback,
            )
        else:
            pipeline = StandardExtractorPipeline(llm_callable=llm_client.extract_check_items_json)
            package = pipeline.run(
                file_name=file.filename or "standard.pdf",
                content=parsed_text,
                manual_category=manual_cat,
            )
        package.standard_info.source_file_url = source_file_url
        downloads = _persist_artifacts(package)
        return {
            "mode": mode,
            "engine_used": llm_client.display_label,
            "parsed_length": len(parsed_text),
            "parsed_preview": parsed_text[:2000],
            "parsed_text": parsed_text,
            "chunks_info": _build_chunks_routing_info(parsed_text, manual_category=manual_cat),
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


@app.get("/api/lexicon")
def get_lexicon():
    """查询 SQLite 中的量检具、物理单位与判定准则校验词库"""
    db = LexiconDB()
    return db.get_full_lexicon_payload()


@app.post("/api/lexicon")
def add_lexicon_keywords(req: LexiconCreateRequest):
    """新增一个或多个校验词库词条（支持逗号/换行批量添加及多语言同义词对照）"""
    db = LexiconDB()
    try:
        added = db.add_keywords(
            req.category,
            req.keyword,
            description=req.description,
            lang=req.lang,
            aliases=req.aliases,
        )
        return {
            "added": added,
            "lexicon": db.get_full_lexicon_payload(),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"添加词条失败: {e}")


@app.put("/api/lexicon/{item_id}")
def update_lexicon_keyword(item_id: int, req: LexiconUpdateRequest):
    """修改指定 ID 的词条内容、语种、多语言同义词或备注说明"""
    db = LexiconDB()
    try:
        updated = db.update_keyword(
            item_id,
            req.keyword,
            description=req.description,
            lang=req.lang,
            aliases=req.aliases,
        )
        return {
            "updated": updated,
            "lexicon": db.get_full_lexicon_payload(),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新词条失败: {e}")


@app.delete("/api/lexicon/{item_id}")
def delete_lexicon_keyword(item_id: int):
    """删除指定 ID 的校验词库词条"""
    db = LexiconDB()
    deleted = db.delete_keyword(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"词条 ID={item_id} 不存在")
    return {
        "deleted_id": item_id,
        "lexicon": db.get_full_lexicon_payload(),
    }


@app.post("/api/lexicon/reset")
def reset_lexicon_keywords(req: LexiconResetRequest):
    """将指定分类或全部 SQLite 词库恢复为出厂默认"""
    db = LexiconDB()
    try:
        payload = db.reset_to_defaults(req.category)
        return payload
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"恢复默认词库失败: {e}")


@app.post("/api/preview-prompt")
def preview_prompt(req: PreviewPromptRequest):
    classifier = StandardClassifier()
    builder = PromptBuilder()
    try:
        if req.mode == "char_list":
            prompt = builder.build_characteristic_list_prompt(context_text=req.content)
            full_prompt = f"{prompt}\n\n{CHAR_LIST_JSON_INSTRUCTION}"
            return {
                "plugin_key": "char_list",
                "standard_category": "特性清单模式 (全类统一)",
                "template_version": "v1.0",
                "prompt": full_prompt,
            }
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
def run_extraction(
    req: ExtractRequest,
    current_user: UserResponse = Depends(get_current_user),
):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="标准文本内容不能为空")

    # 严格使用服务端环境变量配置的大模型 API Key，禁止用户各自配置个人 Key
    llm_client = LLMClient(
        provider=req.llm_provider,
        api_key=None,
        base_url=req.llm_base_url or req.deepseek_base_url,
        model=req.llm_model or req.deepseek_model,
        temperature=req.llm_temperature,
    )

    use_llm = False
    if req.engine_mode in ("deepseek", "llm"):
        if not llm_client.is_configured:
            raise HTTPException(
                status_code=400,
                detail=f"已选择大模型模式，但未检测到服务端 {llm_client.provider_name} API Key 配置。请管理员在服务端 .env 中设置。",
            )
        use_llm = True
    elif req.engine_mode == "auto" and llm_client.is_configured:
        use_llm = True

    try:
        if req.mode == "char_list":
            if use_llm:
                pipeline = StandardExtractorPipeline(llm_callable=llm_client.extract_characteristic_list_json)
                engine_label = f"{llm_client.provider_short_name} API ({llm_client.model}) [特性清单模式]"
                refiner = llm_client.refine_characteristics_with_feedback
            else:
                pipeline = StandardExtractorPipeline(llm_callable=None)
                engine_label = "内置结构化基准引擎 (离线免Key) [特性清单模式]"
                refiner = None
            package = pipeline.run_characteristic_list(
                file_name=req.file_name,
                content=req.content,
                manual_category=req.manual_category or None,
                refiner=refiner,
            )
        else:
            if use_llm:
                pipeline = StandardExtractorPipeline(llm_callable=llm_client.extract_check_items_json)
                engine_label = f"{llm_client.provider_short_name} API ({llm_client.model})"
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
            "mode": req.mode,
            "engine_used": engine_label,
            "package": package.model_dump(mode="json"),
            **downloads,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/approve-review")
def approve_or_reject_review_item(
    req: ReviewUpdateRequest,
    current_user: UserResponse = Depends(get_current_user),
):
    """人工审核门禁交互：质量工程师修改并准入待确认项，或驳回剔除，同时记录至自学习规则引擎并同步刷新 Excel"""
    try:
        is_char_list = "characteristics" in req.package
        if is_char_list:
            char_package = CharacteristicListPackage.model_validate(req.package)
            if req.review_index < 0 or req.review_index >= len(char_package.pending_review_items):
                raise HTTPException(status_code=400, detail="待审核项索引越界")
            orig_char = char_package.pending_review_items.pop(req.review_index)
            if req.action == "approve":
                final_char = CharacteristicEntry.model_validate(req.updated_item) if req.updated_item else orig_char
                final_char.needs_human_review = False
                char_package.characteristics.append(final_char)
            char_package.refresh_summary()
            downloads = _persist_artifacts(char_package)
            return {
                "package": char_package.model_dump(mode="json"),
                "learning_event": None,
                "learning_summary": {},
                **downloads,
            }

        package = ExtractionPackage.model_validate(req.package)
        if req.review_index < 0 or req.review_index >= len(package.pending_review_items):
            raise HTTPException(status_code=400, detail="待审核项索引越界")

        orig_item = package.pending_review_items.pop(req.review_index)
        final_item = orig_item

        if req.action == "approve":
            if req.updated_item:
                final_item = CheckItem.model_validate(req.updated_item)
            final_item.needs_human_review = False
            package.check_items.append(final_item)

        # 记录质量工程师的人工决策至自学习引擎（满 N 次自动改写门禁与提示词规则）
        learner = FeedbackRuleLearner()
        learning_event = learner.record_review_decision(
            standard_category=package.standard_info.standard_category,
            original_item=orig_item,
            action=req.action,
            updated_item=final_item if req.action == "approve" else None,
        )

        package.refresh_summary()
        downloads = _persist_artifacts(package)
        return {
            "package": package.model_dump(mode="json"),
            "learning_event": learning_event,
            "learning_summary": learner.get_full_learning_payload(),
            **downloads,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"审核处理失败: {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"审核操作失败: {e}")


@app.get("/api/learning/rules")
def list_learned_rules():
    """查询人工复核自学习规则列表、学习中候选进度与当前触发阈值"""
    learner = FeedbackRuleLearner()
    return learner.get_full_learning_payload()


@app.put("/api/learning/settings")
def update_learning_settings(req: LearningSettingsRequest):
    """修改自动更改规则的累计次数阈值（如 2 次、3 次、5 次）"""
    learner = FeedbackRuleLearner()
    learner.set_threshold(req.threshold)
    return learner.get_full_learning_payload()


@app.put("/api/learning/rules/{rule_id}")
def update_learned_rule(rule_id: int, req: LearnedRuleUpdateRequest):
    """手动启用/暂停某条自学习规则，或修改其自动回填的检查方法与判定准则"""
    learner = FeedbackRuleLearner()
    try:
        updated = learner.update_rule(
            rule_id=rule_id,
            action=req.action,
            is_active=req.is_active,
            learned_special_char_class=req.learned_special_char_class,
            learned_specification=req.learned_specification,
            learned_method=req.learned_method,
            learned_criteria=req.learned_criteria,
            learned_sample_frequency=req.learned_sample_frequency,
            learned_control_method=req.learned_control_method,
            learned_reaction_plan=req.learned_reaction_plan,
        )
        return {
            "updated": updated,
            "learning": learner.get_full_learning_payload(),
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"更新自学习规则失败: {e}")


@app.delete("/api/learning/rules/{rule_id}")
def delete_learned_rule(rule_id: int):
    """删除并回滚某条自学习规则"""
    learner = FeedbackRuleLearner()
    deleted = learner.delete_rule(rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"自学习规则 ID={rule_id} 不存在")
    return {
        "deleted_id": rule_id,
        "learning": learner.get_full_learning_payload(),
    }


@app.post("/api/learning/reset")
def reset_all_learned_rules():
    """清空所有自学习规则与历史计数"""
    learner = FeedbackRuleLearner()
    learner.reset_all_rules()
    return learner.get_full_learning_payload()


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
