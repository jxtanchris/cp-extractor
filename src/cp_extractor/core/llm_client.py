"""
多后端大语言模型客户端 (Multi-Provider LLM Client)
基于 OpenAI 兼容接口协议，内置支持：
- DeepSeek (deepseek-chat / deepseek-reasoner)
- 阿里云百炼 / 通义千问 Qwen (qwen-plus / qwen-max / qwq-32b)
- 智谱清言 GLM (glm-4-plus / glm-4-air / glm-4-flash)
- 月之暗面 Kimi (moonshot-v1-32k / kimi-latest)
- 硅基流动 SiliconFlow (deepseek-ai/DeepSeek-V3 / Qwen2.5-72B)
- OpenAI (gpt-4o / gpt-4.1 / o3-mini)
- 本地 Ollama / vLLM 私有化部署 (免 API Key)
- 自定义 OpenAI 兼容接口

提供结构化 JSON 提取、截断自恢复、连通性测试与二次语义自纠错能力。
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from dotenv import load_dotenv

load_dotenv()

JSON_SCHEMA_INSTRUCTION = """
--- 严格 JSON 输出格式要求（顺序锚定防幻觉 v2.0 15字段契约） ---
请严格输出合法的 JSON 对象（不要输出多余的解释文字）。
【生成顺序强约束】：每个检查项对象必须严格按照下方字段顺序生成——先定条款号 (`standard_clause`) 与逐字原文引用 (`source_quote`) 作为事实锚点，再基于该原文推导特性名称、公差规范、检查方法与判定准则，严禁脱离原文编造或篡改单边公差：
{
  "items": [
    {
      "standard_clause": "标准条款号（如 3.1）",
      "source_page": 1,
      "source_quote": "原文引用（必须逐字摘录标准原文中的关键原句片段，单条控制在80字以内，不得改写符号或数值）",
      "characteristic_name": "特性名称（控制计划术语）",
      "characteristic_category": "尺寸 | 材料 | 工艺参数 | 外观 | 性能（五选一）",
      "special_char_class": "特殊特性分类（如 CC/SC/PTC/关键特性，仅在原文明确标注时填写，否则填 null）",
      "specification": "规范或公差（保留原文数值、单位、正负号与GD&T符号，单边公差如+0.05/-0.00严禁改写为±0.05）",
      "inspection_method": "检查/试验方法（只填量检具、仪器或试验标准，如三坐标、按GB/T 228.1拉伸试验机、目视等）",
      "acceptance_criteria": "判定准则（只填合格/不合格的明确数值边界或外观状态条件，严禁把试验仪器名称填入此字段）",
      "sample_size_frequency": "样本容量与抽样频次（如 100%、5件/每班、每批首末件，仅在原文明确提及时填写，否则填 null，严禁编造）",
      "control_method": "控制方法（如 SPC控制图、首末件记录表、防错点检表、在线连续监控记录，仅在原文明确提及时填写，否则填 null）",
      "reaction_plan": "反应计划（如 停机隔离、标识不良品并100%追溯，仅在原文明确提及时填写，否则填 null，严禁编造）",
      "confidence": "高 | 中 | 低（三选一）",
      "needs_human_review": false,
      "notes": "备注说明（可选，对模糊或存疑项说明原因）"
    }
  ]
}
【极其重要】：只输出包含上述 JSON 对象的 ```json ``` 代码块，严禁输出任何多余的 Markdown 总结表格、导言、分析过程或解释文字！必须直接以 ```json 开始！
重要完整性约束：表格中的每一行尺寸分段、每一个化学成分元素必须独立生成一条检查项对象（若原文带有 `<!-- 第 X 页 -->` 标记，请将该页码整数填入 `source_page`，否则填 null），确保 JSON 完整闭合不被截断。
"""


LLM_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "key": "deepseek",
        "name": "DeepSeek (深度求索)",
        "short_name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "models": [
            {"id": "deepseek-chat", "label": "deepseek-chat (DeepSeek-V3，推荐)"},
            {"id": "deepseek-reasoner", "label": "deepseek-reasoner (DeepSeek-R1 深度推理)"},
        ],
        "env_key": "DEEPSEEK_API_KEY",
        "requires_key": True,
        "placeholder": "sk-...",
    },
    "qwen": {
        "key": "qwen",
        "name": "通义千问 Qwen (阿里云百炼)",
        "short_name": "Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "models": [
            {"id": "qwen-plus", "label": "qwen-plus (均衡高性价比，推荐)"},
            {"id": "qwen-max", "label": "qwen-max (千问旗舰增强模型)"},
            {"id": "qwen-turbo", "label": "qwen-turbo (高速响应)"},
            {"id": "qwen-long", "label": "qwen-long (超长标准文档专用)"},
            {"id": "qwq-32b", "label": "qwq-32b (深度推理模型)"},
        ],
        "env_key": "DASHSCOPE_API_KEY",
        "requires_key": True,
        "placeholder": "sk-...",
    },
    "zhipu": {
        "key": "zhipu",
        "name": "智谱清言 GLM (BigModel)",
        "short_name": "GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-plus",
        "models": [
            {"id": "glm-4-plus", "label": "glm-4-plus (高智能旗舰，推荐)"},
            {"id": "glm-4-air", "label": "glm-4-air (高性价比)"},
            {"id": "glm-4-flash", "label": "glm-4-flash (高速免费级)"},
            {"id": "glm-z1-air", "label": "glm-z1-air (数理推理增强)"},
        ],
        "env_key": "ZHIPU_API_KEY",
        "requires_key": True,
        "placeholder": "智谱 API Key...",
    },
    "kimi": {
        "key": "kimi",
        "name": "月之暗面 Kimi (Moonshot)",
        "short_name": "Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-32k",
        "models": [
            {"id": "moonshot-v1-32k", "label": "moonshot-v1-32k (长文本标准，推荐)"},
            {"id": "kimi-k2.6", "label": "kimi-k2.6 (Kimi K2.6 旗舰，固定 temperature=1)"},
            {"id": "kimi-k2.5", "label": "kimi-k2.5 (Kimi K2.5 推理，固定 temperature=1)"},
            {"id": "kimi-latest", "label": "kimi-latest (最新演进版)"},
            {"id": "moonshot-v1-128k", "label": "moonshot-v1-128k (超长文档)"},
            {"id": "moonshot-v1-8k", "label": "moonshot-v1-8k (短标准文档)"},
        ],
        "env_key": "MOONSHOT_API_KEY",
        "requires_key": True,
        "placeholder": "sk-...",
    },
    "siliconflow": {
        "key": "siliconflow",
        "name": "硅基流动 (SiliconFlow)",
        "short_name": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "deepseek-ai/DeepSeek-V3",
        "models": [
            {"id": "deepseek-ai/DeepSeek-V3", "label": "deepseek-ai/DeepSeek-V3 (推荐)"},
            {"id": "deepseek-ai/DeepSeek-R1", "label": "deepseek-ai/DeepSeek-R1 (推理)"},
            {"id": "Qwen/Qwen2.5-72B-Instruct", "label": "Qwen/Qwen2.5-72B-Instruct"},
            {"id": "Pro/deepseek-ai/DeepSeek-V3", "label": "Pro/deepseek-ai/DeepSeek-V3"},
        ],
        "env_key": "SILICONFLOW_API_KEY",
        "requires_key": True,
        "placeholder": "sk-...",
    },
    "openai": {
        "key": "openai",
        "name": "OpenAI (GPT-4o / GPT-4.1)",
        "short_name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "models": [
            {"id": "gpt-4o", "label": "gpt-4o (主力全能模型，推荐)"},
            {"id": "gpt-4o-mini", "label": "gpt-4o-mini (轻量快速)"},
            {"id": "gpt-4.1", "label": "gpt-4.1 (新一代高精度模型)"},
            {"id": "gpt-4.1-mini", "label": "gpt-4.1-mini"},
            {"id": "o3-mini", "label": "o3-mini (推理模型)"},
        ],
        "env_key": "OPENAI_API_KEY",
        "requires_key": True,
        "placeholder": "sk-...",
    },
    "ollama": {
        "key": "ollama",
        "name": "本地 Ollama / vLLM (私有化免 Key)",
        "short_name": "Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "default_model": "qwen2.5:14b",
        "models": [
            {"id": "qwen2.5:14b", "label": "qwen2.5:14b (本地推荐)"},
            {"id": "qwen2.5:32b", "label": "qwen2.5:32b"},
            {"id": "deepseek-r1:14b", "label": "deepseek-r1:14b"},
            {"id": "deepseek-r1:32b", "label": "deepseek-r1:32b"},
            {"id": "llama3.1:8b", "label": "llama3.1:8b"},
        ],
        "env_key": "OLLAMA_API_KEY",
        "requires_key": False,
        "placeholder": "本地部署无需 Key（可留空或填 ollama）",
    },
    "custom": {
        "key": "custom",
        "name": "自定义 OpenAI 兼容接口",
        "short_name": "CustomLLM",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "models": [
            {"id": "gpt-4o", "label": "自定义模型（可在下方自由输入模型名）"},
        ],
        "env_key": "CUSTOM_LLM_API_KEY",
        "requires_key": False,
        "placeholder": "sk-...（若内网免鉴权可留空）",
    },
}


def normalize_base_url(raw_base: str) -> str:
    """
    智能规范化不同大模型厂商的 Base URL：
    - 若已包含 /v1, /v2, /v3, /v4（如智谱 /api/paas/v4、百炼 /compatible-mode/v1），原样保留；
    - 若误带了 /chat/completions 后缀，自动剥离；
    - 否则自动追加 /v1（如 https://api.deepseek.com -> https://api.deepseek.com/v1）。
    """
    cleaned = (raw_base or "https://api.deepseek.com").strip().rstrip("/")
    if cleaned.endswith("/chat/completions"):
        cleaned = cleaned[: -len("/chat/completions")].rstrip("/")
    if re.search(r"/v\d+$", cleaned, re.IGNORECASE):
        return cleaned
    return f"{cleaned}/v1"


def infer_provider_from_url_or_model(base_url: str, model: str, explicit_provider: Optional[str] = None) -> str:
    """根据显式标识、Base URL 或模型名识别对应的厂商标识"""
    if explicit_provider and explicit_provider in LLM_PROVIDERS:
        return explicit_provider
    url_lower = (base_url or "").lower()
    model_lower = (model or "").lower()
    if "dashscope.aliyuncs.com" in url_lower or model_lower.startswith("qwen") or model_lower.startswith("qwq"):
        return "qwen"
    if "bigmodel.cn" in url_lower or model_lower.startswith("glm"):
        return "zhipu"
    if "moonshot.cn" in url_lower or "moonshot" in model_lower or model_lower.startswith("kimi"):
        return "kimi"
    if "siliconflow.cn" in url_lower:
        return "siliconflow"
    if "11434" in url_lower or "ollama" in url_lower:
        return "ollama"
    if "openai.com" in url_lower or model_lower.startswith("gpt-"):
        return "openai"
    if "deepseek.com" in url_lower or "deepseek" in model_lower:
        return "deepseek"
    return "custom"


def get_providers_status() -> Dict[str, Any]:
    """读取当前环境变量与全部预设厂商状态，供前端配置弹窗展示"""
    load_dotenv(override=True)
    active_provider = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
    if active_provider not in LLM_PROVIDERS:
        active_provider = "deepseek"

    provider_Meta_list: List[Dict[str, Any]] = []
    for p_key, p_info in LLM_PROVIDERS.items():
        env_var_name = p_info["env_key"]
        env_val = os.getenv(env_var_name, "").strip()
        if p_key == active_provider and not env_val:
            env_val = os.getenv("LLM_API_KEY", "").strip() or os.getenv("DEEPSEEK_API_KEY", "").strip()
        masked = f"{env_val[:6]}...{env_val[-4:]}" if len(env_val) > 10 else ("已配置" if env_val else "")
        provider_Meta_list.append({
            **p_info,
            "has_env_key": bool(env_val) or not p_info["requires_key"],
            "masked_env_key": masked,
        })

    active_info = LLM_PROVIDERS[active_provider]
    default_base_url = (
        os.getenv("LLM_BASE_URL", "").strip()
        or (os.getenv("DEEPSEEK_BASE_URL", "").strip() if active_provider == "deepseek" else "")
        or active_info["base_url"]
    )
    default_model = (
        os.getenv("LLM_MODEL", "").strip()
        or (os.getenv("DEEPSEEK_MODEL", "").strip() if active_provider == "deepseek" else "")
        or active_info["default_model"]
    )
    active_env_key = (
        os.getenv(active_info["env_key"], "").strip()
        or os.getenv("LLM_API_KEY", "").strip()
        or os.getenv("DEEPSEEK_API_KEY", "").strip()
    )
    raw_temp = os.getenv("LLM_TEMPERATURE", "0.0").strip()
    try:
        default_temp = float(raw_temp)
    except ValueError:
        default_temp = 0.0

    return {
        "active_provider": active_provider,
        "has_env_key": bool(active_env_key) or not active_info["requires_key"],
        "masked_env_key": f"{active_env_key[:6]}...{active_env_key[-4:]}" if len(active_env_key) > 10 else "",
        "default_base_url": default_base_url,
        "default_model": default_model,
        "default_temperature": default_temp,
        "providers": provider_Meta_list,
    }


def persist_llm_config_to_env(
    env_path: Path,
    provider: str,
    api_key: Optional[str],
    base_url: str,
    model: str,
    temperature: Optional[float] = None,
) -> None:
    """将当前选中的大模型厂商、Base URL、Model、Temperature 及对应 API Key 持久化写入项目根目录 .env 文件"""
    p_key = provider if provider in LLM_PROVIDERS else "custom"
    p_info = LLM_PROVIDERS[p_key]
    provider_env_key = p_info["env_key"]

    updates: Dict[str, str] = {
        "LLM_PROVIDER": p_key,
        "LLM_BASE_URL": base_url.strip(),
        "LLM_MODEL": model.strip(),
    }
    if temperature is not None:
        updates["LLM_TEMPERATURE"] = str(round(float(temperature), 2))
    clean_key = (api_key or "").strip()
    if clean_key:
        updates["LLM_API_KEY"] = clean_key
        updates[provider_env_key] = clean_key
        if p_key == "deepseek":
            updates["DEEPSEEK_API_KEY"] = clean_key

    if p_key == "deepseek":
        updates["DEEPSEEK_BASE_URL"] = base_url.strip()
        updates["DEEPSEEK_MODEL"] = model.strip()

    existing_lines: List[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    updated_keys = set()
    new_lines: List[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in updates:
            new_lines.append(f"{k}={updates[k]}")
            updated_keys.add(k)
        else:
            new_lines.append(line)

    for k, v in updates.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    load_dotenv(dotenv_path=env_path, override=True)


class LLMClient:
    """通用 OpenAI 兼容多模型客户端（支持 DeepSeek、Qwen、GLM、Kimi、SiliconFlow、OpenAI、Ollama 等）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: float = 180.0,
    ):
        load_dotenv(override=True)
        env_provider = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
        resolved_provider = (provider or env_provider or "deepseek").strip().lower()
        if resolved_provider not in LLM_PROVIDERS:
            resolved_provider = infer_provider_from_url_or_model(base_url or "", model or "")

        p_info = LLM_PROVIDERS.get(resolved_provider, LLM_PROVIDERS["deepseek"])
        self.provider = resolved_provider
        self.provider_name = p_info["name"]
        self.provider_short_name = p_info["short_name"]
        self.requires_key = bool(p_info.get("requires_key", True))

        # 解析环境变量中的 API Key（优先查找当前厂商专属 Key，其次通用 LLM_API_KEY / DEEPSEEK_API_KEY）
        provider_env_key = os.getenv(p_info["env_key"], "").strip()
        generic_env_key = os.getenv("LLM_API_KEY", "").strip()
        ds_env_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.env_api_key = provider_env_key or generic_env_key or (ds_env_key if self.provider == "deepseek" else "")

        raw_key = (api_key or self.env_api_key).strip()
        if not raw_key and not self.requires_key:
            raw_key = "ollama"
        self.api_key = raw_key

        raw_base = (
            base_url
            or (os.getenv("LLM_BASE_URL", "").strip() if not provider or provider == env_provider else "")
            or (os.getenv("DEEPSEEK_BASE_URL", "").strip() if self.provider == "deepseek" else "")
            or p_info["base_url"]
        )
        self.base_url = normalize_base_url(raw_base)

        self.model = (
            model
            or (os.getenv("LLM_MODEL", "").strip() if not provider or provider == env_provider else "")
            or (os.getenv("DEEPSEEK_MODEL", "").strip() if self.provider == "deepseek" else "")
            or p_info["default_model"]
        ).strip()

        if temperature is not None:
            self.temperature = float(temperature)
        else:
            env_temp_str = os.getenv("LLM_TEMPERATURE", "").strip()
            try:
                self.temperature = float(env_temp_str) if env_temp_str else 0.0
            except ValueError:
                self.temperature = 0.0

        # 对要求固定 temperature=1 的模型（如 kimi-k2.5 / kimi-k2.6 / kimi-thinking）自动适配为 1.0
        if self._requires_temperature_one() and self.temperature != 1.0:
            self.temperature = 1.0

        self.timeout = timeout

    def _requires_temperature_one(self) -> bool:
        """检测是否属于强制要求 temperature=1 的模型（如 Kimi K2 系列、o1/o3 系列等）"""
        m = self.model.lower()
        return any(tag in m for tag in ("kimi-k2", "kimi-thinking", "k2.5", "k2.6", "o1", "o3"))

    @property
    def is_configured(self) -> bool:
        if not self.requires_key:
            return True
        return bool(self.api_key)

    @property
    def display_label(self) -> str:
        return f"{self.provider_short_name} ({self.model})"

    def test_connection(self) -> Dict[str, Any]:
        """发送轻量探针请求测试当前模型配置连通性与响应耗时（自动兼容特殊 temperature 约束）"""
        if self.requires_key and not self.api_key:
            raise ValueError(f"未配置 {self.provider_name} 的 API Key")

        t0 = time.perf_counter()
        messages = [{"role": "user", "content": "请仅回复 OK 两个字母。"}]
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 32,
            "temperature": self.temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=25.0) as client:
            resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            # 若目标模型对 temperature 有特殊限制（如 Kimi k2.6 报错 "invalid temperature: only 1 is allowed for this model"），自动切换为 temperature=1 重试
            if resp.status_code == 400 and "temperature" in resp.text.lower():
                self.temperature = 1.0
                payload["temperature"] = 1
                resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"{self.provider_short_name} 接口返回 HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            msg_obj = data["choices"][0]["message"]
            reply = (msg_obj.get("content") or msg_obj.get("reasoning_content") or "").strip()
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "ok": True,
            "provider": self.provider,
            "provider_name": self.provider_name,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "reply": reply[:40] or "OK",
            "latency_ms": latency_ms,
        }

    def extract_check_items_json(self, assembled_prompt: str) -> str:
        """
        调用当前配置的大语言模型执行检查项提取，返回 JSON 数组字符串（供 StandardExtractorPipeline 解析）。
        """
        if not self.is_configured:
            raise ValueError(
                f"未配置 {self.provider_name} API Key。请在 Web 界面右上角【大模型 API 配置】中填入 Key 或在 .env 中设置。"
            )

        full_user_prompt = f"{assembled_prompt}\n\n{JSON_SCHEMA_INSTRUCTION}"
        messages: List[Dict[str, str]] = [
            {
                "role": "system",
                "content": "你是严谨的汽车行业 IATF 16949 质量工程师，专门从技术标准中提取可溯源的控制计划检查项并严格输出 JSON。",
            },
            {"role": "user", "content": full_user_prompt},
        ]
        content = self._call_chat_completions(messages)
        items_list = self._parse_items_from_text(content)
        return json.dumps(items_list, ensure_ascii=False)

    def refine_items_with_feedback(
        self,
        source_text: str,
        failed_items: List[Dict[str, Any]],
        error_messages: List[str],
        validation_instruction: str = "",
    ) -> List[Dict[str, Any]]:
        """
        二次语义校验与打回修正闭环 (Self-Correction)：
        针对未通过确定性质量门禁（如原文引用改写/幻觉、单边公差被篡改为 ±、试验方法与判定准则混淆）的检查项，
        结合原标准文本和具体错误原因，调用 LLM 进行定向修正。
        """
        if not self.is_configured or not failed_items:
            return failed_items

        val_header = validation_instruction.strip() or "请根据标准原文和质量门禁报错信息，逐条修正以下存在问题的检查项。"
        feedback_prompt = (
            f"{val_header}\n\n"
            "--- 质量门禁检测到的具体错误 ---\n"
            + "\n".join(f"- {msg}" for msg in error_messages)
            + "\n\n--- 待修正的检查项 JSON ---\n"
            + json.dumps({"items": failed_items}, ensure_ascii=False, indent=2)
            + "\n\n--- 标准原文参考 ---\n"
            + source_text
            + "\n\n请严格基于标准原文修正上述条目：\n"
            "1. `source_quote` 必须是标准原文中一字不差的真实子串；\n"
            "2. 若原文为单边公差（如 `+0.05/-0.00`），`specification` 必须恢复为单边公差，严禁写成 `±`；\n"
            "3. `inspection_method` 只填量检具/试验仪器，`acceptance_criteria` 只填合格判据数值或状态边界；\n"
            "4. 若某条确系括号参考尺寸（如 `(120)`）或非受控设备维护要求，请将其从结果中剔除；若原文本身就是待定/参考项，请保留并将 `confidence` 设为 `\"低\"`、`needs_human_review` 设为 `true`。\n"
            f"{JSON_SCHEMA_INSTRUCTION}"
        )
        messages: List[Dict[str, str]] = [
            {
                "role": "system",
                "content": "你是严谨的汽车行业 IATF 16949 质量审核专家，负责对提取存在偏差的控制计划检查项进行精准溯源修正并输出 JSON。",
            },
            {"role": "user", "content": feedback_prompt},
        ]
        content = self._call_chat_completions(messages)
        return self._parse_items_from_text(content)

    def _is_reasoning_model(self) -> bool:
        m = self.model.lower()
        return any(tag in m for tag in ("reasoner", "deepseek-r1", "qwq", "glm-z1", "o1", "o3"))

    def _call_chat_completions(self, messages: List[Dict[str, str]]) -> str:
        """底层 HTTP 调用封装（自适应不同大模型厂商的参数约束）"""
        is_reasoning = self._is_reasoning_model()
        max_out_tokens = 4096 if "8k" in self.model.lower() else 8192

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_out_tokens,
        }
        if not is_reasoning:
            payload["temperature"] = self.temperature

        # 非推理模型且非本地 Ollama 默认启用 json_object 约束；若厂商不支持将在下方自动降级重试
        if not is_reasoning and not self._requires_temperature_one() and self.provider not in ("ollama",):
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)

            # 若前端浏览器缓存了过期/无效的 API Key (HTTP 401)，且服务端 .env 配置了不同的 Key，自动回退使用 .env Key 重试
            if resp.status_code == 401 and self.env_api_key and self.env_api_key != self.api_key:
                self.api_key = self.env_api_key
                headers["Authorization"] = f"Bearer {self.api_key}"
                resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)

            # 若模型强制要求 temperature=1（如 Kimi k2.6 返回 "invalid temperature: only 1 is allowed for this model"），自动修正为 temperature=1 重试
            if resp.status_code == 400 and "temperature" in resp.text.lower():
                self.temperature = 1.0
                payload["temperature"] = 1
                payload.pop("response_format", None)
                resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)

            # 若用户在 DeepSeek 下误填了非官方模型名，自动回退至 deepseek-chat 重试
            if (
                resp.status_code == 400
                and self.provider == "deepseek"
                and "Model Not Exist" in resp.text
                and payload["model"] != "deepseek-chat"
            ):
                payload["model"] = "deepseek-chat"
                payload["response_format"] = {"type": "json_object"}
                resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)

            # 兼容性降级：若部分第三方模型因不支持 response_format 报 HTTP 400，优先剥离 response_format 重试（保留 max_tokens=8192）
            if resp.status_code == 400 and "response_format" in payload:
                fallback_payload = dict(payload)
                fallback_payload.pop("response_format", None)
                resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=fallback_payload)

            # 若依然报 400 且由于 max_tokens 过大导致（例如某些受限提供商限制 max_tokens <= 4096），调小 max_tokens 重试
            if resp.status_code == 400 and payload.get("max_tokens", 0) > 4096:
                fallback_payload = dict(payload)
                fallback_payload.pop("response_format", None)
                fallback_payload["max_tokens"] = 4096
                resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=fallback_payload)

            if resp.status_code != 200:
                detail = resp.text[:400]
                raise RuntimeError(f"{self.provider_short_name} API 调用失败 (HTTP {resp.status_code}): {detail}")

            data = resp.json()
            choice = data["choices"][0]
            message = choice.get("message", {})
            content = message.get("content") or ""
            finish_reason = choice.get("finish_reason")

            # 思考型模型（如 deepseek-flash / reasoner）可能把全部输出 Token 耗在思维链上，正文为空。
            # DeepSeek 下自动切换到非思考模型 deepseek-chat 重试一次，避免用户必须手动切换模型。
            if not content.strip() and finish_reason == "length" and self.provider == "deepseek" and payload["model"] != "deepseek-chat":
                retry_payload = dict(payload)
                retry_payload["model"] = "deepseek-chat"
                retry_payload["temperature"] = self.temperature if self.temperature <= 1.0 else 0.0
                retry_payload["response_format"] = {"type": "json_object"}
                retry_resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=retry_payload)
                if retry_resp.status_code == 200:
                    choice = retry_resp.json()["choices"][0]
                    message = choice.get("message", {})
                    content = message.get("content") or ""
                    finish_reason = choice.get("finish_reason")
                    # 记住回退结果：同一次提取的后续分块直接使用 deepseek-chat，避免每块都白白耗尽思考 Token
                    if content.strip():
                        self.model = "deepseek-chat"

            # 仅当思维链中确实含有检查项 JSON 时才用作兜底（否则会把推理草稿误当 JSON 解析）
            if not content.strip():
                reasoning = message.get("reasoning_content") or ""
                if '"characteristic_name"' in reasoning:
                    content = reasoning

            if not content.strip():
                if finish_reason == "length":
                    raise ValueError(
                        f"当前模型（{self.model}）把输出 Token 全部耗在了深度思考上，正文为空。"
                        "请在【切换模型】中改用非思考模型（如 deepseek-chat）后重试。"
                    )
                raise ValueError(f"大模型返回了空内容 (finish_reason={finish_reason})")

            return content

    @staticmethod
    def _extract_items_list(obj: Any) -> Optional[List[Dict[str, Any]]]:
        """从解析后的 JSON 对象中提取检查项列表，兼容各类模型自定义包装键名与嵌套结构"""
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            # 常见候选列表键名
            candidate_keys = (
                "items",
                "check_items",
                "data",
                "results",
                "extracted_characteristics",
                "characteristics",
                "checkItems",
                "extracted_items",
                "inspection_items",
                "summary_table",
                "table",
                "records",
            )
            for key in candidate_keys:
                if key in obj and isinstance(obj[key], list):
                    return obj[key]
            # 启发式兜底：遍历字典的所有值，寻找包含检查项特征的列表
            for v in obj.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    if any(k in v[0] for k in ("characteristic_name", "specification", "item_name", "name")):
                        return v
        return None

    @staticmethod
    def _salvage_truncated_json(text: str) -> List[Dict[str, Any]]:
        """
        当大模型因输出超长 (max_tokens) 导致 JSON 末尾截断（报 Unterminated string 等错误）时，
        自动抢救并提取截断前所有已完整闭合的检查项对象。
        """
        search_pos = len(text)
        while search_pos > 0:
            brace_idx = text.rfind("}", 0, search_pos)
            if brace_idx == -1:
                break
            candidate_prefix = text[: brace_idx + 1].rstrip().rstrip(",")
            for suffix in ("", "]", "]}"):
                try:
                    obj = json.loads(candidate_prefix + suffix)
                    items = LLMClient._extract_items_list(obj)
                    if items is not None:
                        return items
                except json.JSONDecodeError:
                    pass
            search_pos = brace_idx

        decoder = json.JSONDecoder()
        items: List[Dict[str, Any]] = []
        idx = 0
        while idx < len(text):
            start = text.find("{", idx)
            if start == -1:
                break
            try:
                obj, end = decoder.raw_decode(text, start)
                if isinstance(obj, dict) and any(k in obj for k in ("characteristic_name", "specification", "item_name")):
                    items.append(obj)
                    idx = end
                    continue
            except json.JSONDecodeError:
                pass
            idx = start + 1

        if items:
            return items
        raise ValueError("大模型返回的 JSON 因超长截断且无法恢复有效条目，请尝试拆分文档后重试。")

    @staticmethod
    def _parse_items_from_text(text: str) -> List[Dict[str, Any]]:
        """从模型响应文本中安全解析并归一化检查项数组（支持 <think> 思维链剥离与超长截断自动恢复）"""
        cleaned = text.strip()
        # 剥离推理模型（如 DeepSeek-R1 / QwQ / 本地 R1）可能输出的 <think>...</think> 标签
        cleaned = re.sub(r"<think>[\s\S]*?</think>", "", cleaned).strip()

        code_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
        if code_match:
            cleaned = code_match.group(1).strip()
        elif cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned).strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            parsed = LLMClient._salvage_truncated_json(cleaned)

        items = LLMClient._extract_items_list(parsed)
        if items is not None:
            parsed = items
        elif isinstance(parsed, dict):
            parsed = [parsed]

        if not isinstance(parsed, list):
            raise ValueError("大模型返回的 JSON 结构无法解析为检查项列表")

        valid_categories = {"尺寸", "材料", "工艺参数", "外观", "性能"}
        category_alias = {
            "尺寸公差": "尺寸",
            "形位公差": "尺寸",
            "几何公差": "尺寸",
            "工艺": "工艺参数",
            "过程参数": "工艺参数",
            "过程特性": "工艺参数",
            "理化性能": "性能",
            "力学性能": "材料",
            "化学成分": "材料",
        }
        valid_conf = {"高", "中", "低"}
        conf_alias = {"high": "高", "medium": "中", "low": "低"}

        normalized: List[Dict[str, Any]] = []
        for raw_item in parsed:
            if not isinstance(raw_item, dict):
                continue
            cat = str(raw_item.get("characteristic_category", "尺寸")).strip()
            if cat not in valid_categories:
                cat = category_alias.get(cat, "尺寸")

            conf = str(raw_item.get("confidence", "高")).strip()
            if conf not in valid_conf:
                conf = conf_alias.get(conf.lower(), "中")

            raw_page = raw_item.get("source_page")
            parsed_page: Optional[int] = None
            if raw_page is not None:
                try:
                    p_int = int(str(raw_page).strip())
                    if p_int >= 1:
                        parsed_page = p_int
                except (ValueError, TypeError):
                    pass

            def _clean_opt_str(val: Any) -> Optional[str]:
                if val is None:
                    return None
                s = str(val).strip()
                if not s or s.lower() in ("null", "none", "n/a") or s in ("无", "-"):
                    return None
                return s

            normalized.append({
                "characteristic_name": str(raw_item.get("characteristic_name", "未命名特性")),
                "characteristic_category": cat,
                "special_char_class": _clean_opt_str(raw_item.get("special_char_class")),
                "specification": str(raw_item.get("specification", "")),
                "inspection_method": str(raw_item.get("inspection_method", "待确认")),
                "acceptance_criteria": str(raw_item.get("acceptance_criteria", "")),
                "sample_size_frequency": _clean_opt_str(raw_item.get("sample_size_frequency")),
                "control_method": _clean_opt_str(raw_item.get("control_method")),
                "reaction_plan": _clean_opt_str(raw_item.get("reaction_plan")),
                "standard_clause": str(raw_item.get("standard_clause", "")),
                "source_page": parsed_page,
                "source_quote": str(raw_item.get("source_quote", "")),
                "confidence": conf,
                "needs_human_review": bool(raw_item.get("needs_human_review", conf == "低")),
                "notes": raw_item.get("notes"),
            })

        return normalized


# 保持向后兼容别名
DeepSeekClient = LLMClient
