"""
DeepSeek 大语言模型客户端
基于 OpenAI 兼容接口协议，支持 deepseek-chat (V3) 与 deepseek-reasoner (R1) 模型，
提供结构化 JSON 提取与二次语义校验能力。
"""

import json
import os
import re
from typing import Any, Dict, List, Optional
import httpx
from dotenv import load_dotenv

load_dotenv()

JSON_SCHEMA_INSTRUCTION = """
--- 严格 JSON 输出格式要求（顺序锚定防幻觉） ---
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
      "specification": "规范或公差（保留原文数值、单位、正负号与GD&T符号，单边公差如+0.05/-0.00严禁改写为±0.05）",
      "inspection_method": "检查/试验方法（只填量检具、仪器或试验标准，如三坐标、按GB/T 228.1拉伸试验机、目视等）",
      "acceptance_criteria": "判定准则（只填合格/不合格的明确数值边界或外观状态条件，严禁把试验仪器名称填入此字段）",
      "confidence": "高 | 中 | 低（三选一）",
      "needs_human_review": false,
      "notes": "备注说明（可选，对模糊或存疑项说明原因）"
    }
  ]
}
重要完整性约束：表格中的每一行尺寸分段、每一个化学成分元素必须独立生成一条检查项对象（若原文带有 `<!-- 第 X 页 -->` 标记，请将该页码整数填入 `source_page`，否则填 null），确保 JSON 完整闭合不被截断。
"""


class DeepSeekClient:
    """DeepSeek API 封装客户端"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 180.0,
    ):
        load_dotenv(override=True)
        self.env_api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.api_key = (api_key or self.env_api_key).strip()
        raw_base = (base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).strip().rstrip("/")
        if raw_base.endswith("/v1"):
            self.base_url = raw_base
        else:
            self.base_url = f"{raw_base}/v1"
        self.model = (model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")).strip()
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def extract_check_items_json(self, assembled_prompt: str) -> str:
        """
        调用 DeepSeek 接口执行检查项提取，返回 JSON 数组字符串（供 StandardExtractorPipeline 解析）。
        """
        if not self.api_key:
            raise ValueError(
                "未配置 DeepSeek API Key。请在 .env 文件中设置 DEEPSEEK_API_KEY 或在 Web 界面右上角填入 API Key。"
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
        if not self.api_key or not failed_items:
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

    def _call_chat_completions(self, messages: List[Dict[str, str]]) -> str:
        """底层 HTTP 调用封装（temperature=0.0 贪婪解码确保数值与符号最高确定性）"""
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 8192,
        }

        # deepseek-chat 支持 json_object 强制模式；deepseek-reasoner 不支持 response_format 参数
        if "reasoner" not in self.model.lower():
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

            # 若用户在 .env 中误填了 deepseek-flash 等非官方模型名，自动回退至 deepseek-chat 重试
            if resp.status_code == 400 and "Model Not Exist" in resp.text and payload["model"] != "deepseek-chat":
                payload["model"] = "deepseek-chat"
                payload["response_format"] = {"type": "json_object"}
                resp = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)

            if resp.status_code != 200:
                detail = resp.text[:400]
                raise RuntimeError(f"DeepSeek API 调用失败 (HTTP {resp.status_code}): {detail}")

            data = resp.json()
            return data["choices"][0]["message"]["content"]

    @staticmethod
    def _salvage_truncated_json(text: str) -> List[Dict[str, Any]]:
        """
        当大模型因输出超长 (max_tokens) 导致 JSON 末尾截断（报 Unterminated string 等错误）时，
        自动抢救并提取截断前所有已完整闭合的检查项对象。
        """
        # 1. 尝试寻找最后一个完整的对象闭合位置 `}` 并补齐 `]}`
        search_pos = len(text)
        while search_pos > 0:
            brace_idx = text.rfind("}", 0, search_pos)
            if brace_idx == -1:
                break
            candidate_prefix = text[: brace_idx + 1].rstrip().rstrip(",")
            for suffix in ("", "]", "]}"):
                try:
                    obj = json.loads(candidate_prefix + suffix)
                    if isinstance(obj, dict):
                        for key in ("items", "check_items", "data", "results"):
                            if key in obj and isinstance(obj[key], list):
                                return obj[key]
                    elif isinstance(obj, list):
                        return obj
                except json.JSONDecodeError:
                    pass
            search_pos = brace_idx

        # 2. 备选：使用正则/raw_decode 逐个扫描包含 characteristic_name 的 JSON 对象
        decoder = json.JSONDecoder()
        items: List[Dict[str, Any]] = []
        idx = 0
        while idx < len(text):
            start = text.find("{", idx)
            if start == -1:
                break
            try:
                obj, end = decoder.raw_decode(text, start)
                if isinstance(obj, dict) and "characteristic_name" in obj:
                    items.append(obj)
                    idx = end
                    continue
            except json.JSONDecodeError:
                pass
            idx = start + 1

        if items:
            return items
        raise ValueError("DeepSeek 返回的 JSON 因超长截断且无法恢复有效条目，请尝试拆分文档后重试。")

    @staticmethod
    def _parse_items_from_text(text: str) -> List[Dict[str, Any]]:
        """从模型响应文本中安全解析并归一化检查项数组（支持超长截断自动恢复）"""
        cleaned = text.strip()
        # 去除可能包裹的 ```json ... ``` 代码块
        code_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
        if code_match:
            cleaned = code_match.group(1).strip()
        elif cleaned.startswith("```"):
            # 若代码块末尾因超长被截断，没有闭合的 ```，剥离首行 ```json
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned).strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            parsed = DeepSeekClient._salvage_truncated_json(cleaned)
        if isinstance(parsed, dict):
            for key in ("items", "check_items", "data", "results"):
                if key in parsed and isinstance(parsed[key], list):
                    parsed = parsed[key]
                    break
            else:
                parsed = [parsed]

        if not isinstance(parsed, list):
            raise ValueError("DeepSeek 返回的 JSON 结构无法解析为检查项列表")

        # 归一化枚举字段容错
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

            normalized.append({
                "characteristic_name": str(raw_item.get("characteristic_name", "未命名特性")),
                "characteristic_category": cat,
                "specification": str(raw_item.get("specification", "")),
                "inspection_method": str(raw_item.get("inspection_method", "待确认")),
                "acceptance_criteria": str(raw_item.get("acceptance_criteria", "")),
                "standard_clause": str(raw_item.get("standard_clause", "")),
                "source_page": parsed_page,
                "source_quote": str(raw_item.get("source_quote", "")),
                "confidence": conf,
                "needs_human_review": bool(raw_item.get("needs_human_review", conf == "低")),
                "notes": raw_item.get("notes"),
            })

        return normalized
