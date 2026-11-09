"""
人工复核反馈自学习引擎 (Human-in-the-Loop Feedback & Rule Evolution Engine)

核心职责：
1. 观察并记录质量工程师在「待人工确认项」上的每一次手工决策（确认准入 approve / 驳回剔除 reject）及修订内容；
2. 当同一特性/模式累计被确认准入（或驳回）达到设定阈值 N 次（默认 3 次，可配置）时，自动晋升为生效规则；
3. 自动作用于两处核心链路：
   - 门禁层 (QualityGateValidator)：自动放行匹配项、自动回填工程师习惯的检查方法与判定准则（或自动剔除高频驳回项）；
   - 提示词层 (PromptBuilder)：自动将该规则作为 Few-Shot 黄金正/反例注入对应类别的提取提示词中；
   - 词库层 (LexiconDB)：自动将工程师新填写的量检具名称扩充进 SQLite 校验词库。
"""

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..schemas.models import CheckItem
from .lexicon_db import DEFAULT_DB_PATH, LexiconDB

CATEGORY_TO_PLUGIN_KEY = {
    "公差/尺寸标准": "tolerance",
    "tolerance": "tolerance",
    "材料标准": "material",
    "material": "material",
    "工艺标准": "process",
    "process": "process",
    "外观/性能标准": "appearance_performance",
    "appearance_performance": "appearance_performance",
}

PLUGIN_KEY_TO_CATEGORY_LABEL = {
    "tolerance": "公差/尺寸标准",
    "material": "材料标准",
    "process": "工艺标准",
    "appearance_performance": "外观/性能标准",
}


class FeedbackRuleLearner:
    """基于 SQLite 的人工复核自学习与规则演进管理器"""

    DEFAULT_THRESHOLD = 3

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO learning_settings (key, value)
                VALUES ('auto_promote_threshold', ?)
                """,
                (str(self.DEFAULT_THRESHOLD),),
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learned_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_key TEXT NOT NULL UNIQUE,
                    plugin_key TEXT NOT NULL,
                    standard_category TEXT NOT NULL,
                    characteristic_name TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT 'approve',
                    approve_count INTEGER NOT NULL DEFAULT 0,
                    reject_count INTEGER NOT NULL DEFAULT 0,
                    learned_special_char_class TEXT NOT NULL DEFAULT '',
                    learned_specification TEXT NOT NULL DEFAULT '',
                    learned_method TEXT NOT NULL DEFAULT '',
                    learned_criteria TEXT NOT NULL DEFAULT '',
                    learned_sample_frequency TEXT NOT NULL DEFAULT '',
                    learned_control_method TEXT NOT NULL DEFAULT '',
                    learned_reaction_plan TEXT NOT NULL DEFAULT '',
                    sample_clause TEXT NOT NULL DEFAULT '',
                    sample_quote TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 0,
                    promoted_at TEXT DEFAULT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
                )
                """
            )
            # 兼容已有 SQLite 表平滑迁移新增 v2.0 IATF 16949 字段列
            existing_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(learned_rules)").fetchall()
            }
            for col_name in (
                "learned_special_char_class",
                "learned_sample_frequency",
                "learned_control_method",
                "learned_reaction_plan",
            ):
                if col_name not in existing_cols:
                    conn.execute(
                        f"ALTER TABLE learned_rules ADD COLUMN {col_name} TEXT NOT NULL DEFAULT ''"
                    )
            conn.commit()

    @staticmethod
    def normalize_characteristic_name(name: str) -> str:
        """
        对特性名称进行归一化处理，消除空白符、全半角括号等细微差异，
        确保同一检查项在不同批次文档中能稳定聚合计数。
        """
        cleaned = (name or "").strip()
        cleaned = cleaned.replace("（", "(").replace("）", ")")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.lower()

    @classmethod
    def extract_core_characteristic_name(cls, name: str) -> str:
        """
        剥离特性名称前导的【...工序】或[...]前缀后返回归一化核心名称，
        用于严格同源匹配（禁止宽泛子串模糊包含匹配）。
        """
        norm = cls.normalize_characteristic_name(name)
        core = re.sub(r"^(?:【[^】]+】|\[[^\]]+\])\s*", "", norm).strip()
        return core or norm

    @classmethod
    def resolve_category_pair(cls, raw_category: str) -> Tuple[str, str]:
        """返回 (plugin_key, standard_category_label)"""
        cleaned = (raw_category or "").strip()
        plugin_key = CATEGORY_TO_PLUGIN_KEY.get(cleaned, "tolerance")
        label = PLUGIN_KEY_TO_CATEGORY_LABEL.get(plugin_key, "公差/尺寸标准")
        return plugin_key, label

    @classmethod
    def build_pattern_key(cls, raw_category: str, characteristic_name: str) -> str:
        plugin_key, _ = cls.resolve_category_pair(raw_category)
        norm_name = cls.normalize_characteristic_name(characteristic_name)
        return f"{plugin_key}::{norm_name}"

    def get_threshold(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM learning_settings WHERE key = 'auto_promote_threshold'"
            ).fetchone()
            if row:
                try:
                    return max(1, int(row["value"]))
                except ValueError:
                    pass
        return self.DEFAULT_THRESHOLD

    def set_threshold(self, threshold: int) -> int:
        val = max(1, min(20, int(threshold)))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO learning_settings (key, value)
                VALUES ('auto_promote_threshold', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(val),),
            )
            # 当调低阈值时，自动检查已有累积记录是否已达到新阈值并自动升格
            conn.execute(
                """
                UPDATE learned_rules
                SET is_active = 1,
                    action = 'approve',
                    promoted_at = COALESCE(promoted_at, datetime('now', 'localtime')),
                    updated_at = datetime('now', 'localtime')
                WHERE is_active = 0 AND approve_count >= ? AND approve_count > reject_count
                """,
                (val,),
            )
            conn.execute(
                """
                UPDATE learned_rules
                SET is_active = 1,
                    action = 'reject',
                    promoted_at = COALESCE(promoted_at, datetime('now', 'localtime')),
                    updated_at = datetime('now', 'localtime')
                WHERE is_active = 0 AND reject_count >= ? AND reject_count > approve_count
                """,
                (val,),
            )
            conn.commit()
        return val

    def record_review_decision(
        self,
        standard_category: str,
        original_item: CheckItem,
        action: str,
        updated_item: Optional[CheckItem] = None,
    ) -> Dict[str, Any]:
        """
        记录一次质量工程师的手工审核操作（approve 或 reject），并在达到阈值时自动晋升规则。
        """
        if action not in ("approve", "reject"):
            raise ValueError(f"不支持的审核动作: {action}")

        final_item = updated_item or original_item
        plugin_key, category_label = self.resolve_category_pair(standard_category)
        pattern_key = self.build_pattern_key(plugin_key, final_item.characteristic_name)
        threshold = self.get_threshold()

        spec_val = (final_item.specification or "").strip()
        method_val = (final_item.inspection_method or "").strip()
        criteria_val = (final_item.acceptance_criteria or "").strip()
        special_cls_val = (final_item.special_char_class or "").strip()
        sample_freq_val = (final_item.sample_size_frequency or "").strip()
        ctrl_method_val = (final_item.control_method or "").strip()
        reaction_val = (final_item.reaction_plan or "").strip()
        clause_val = (final_item.standard_clause or "").strip()
        quote_val = (final_item.source_quote or "").strip()

        just_promoted = False
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM learned_rules WHERE pattern_key = ?",
                (pattern_key,),
            ).fetchone()

            if existing is None:
                app_cnt = 1 if action == "approve" else 0
                rej_cnt = 1 if action == "reject" else 0
                active_cnt = app_cnt if action == "approve" else rej_cnt
                is_active = 1 if active_cnt >= threshold else 0
                just_promoted = bool(is_active)
                cursor = conn.execute(
                    """
                    INSERT INTO learned_rules (
                        pattern_key, plugin_key, standard_category, characteristic_name,
                        action, approve_count, reject_count,
                        learned_special_char_class, learned_specification, learned_method, learned_criteria,
                        learned_sample_frequency, learned_control_method, learned_reaction_plan,
                        sample_clause, sample_quote, is_active, promoted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 1 THEN datetime('now', 'localtime') ELSE NULL END)
                    """,
                    (
                        pattern_key,
                        plugin_key,
                        category_label,
                        final_item.characteristic_name.strip(),
                        action,
                        app_cnt,
                        rej_cnt,
                        special_cls_val,
                        spec_val,
                        method_val,
                        criteria_val,
                        sample_freq_val,
                        ctrl_method_val,
                        reaction_val,
                        clause_val,
                        quote_val,
                        is_active,
                        is_active,
                    ),
                )
                rule_id = cursor.lastrowid
            else:
                rule_id = existing["id"]
                app_cnt = int(existing["approve_count"]) + (1 if action == "approve" else 0)
                rej_cnt = int(existing["reject_count"]) + (1 if action == "reject" else 0)
                was_active = bool(existing["is_active"])
                active_cnt = app_cnt if action == "approve" else rej_cnt
                dominant_action = "approve" if app_cnt >= rej_cnt else "reject"
                now_active = 1 if (was_active or active_cnt >= threshold) else 0
                if not was_active and now_active == 1:
                    just_promoted = True

                conn.execute(
                    """
                    UPDATE learned_rules
                    SET characteristic_name = ?,
                        action = ?,
                        approve_count = ?,
                        reject_count = ?,
                        learned_special_char_class = CASE WHEN ? != '' THEN ? ELSE learned_special_char_class END,
                        learned_specification = CASE WHEN ? != '' THEN ? ELSE learned_specification END,
                        learned_method = CASE WHEN ? != '' THEN ? ELSE learned_method END,
                        learned_criteria = CASE WHEN ? != '' THEN ? ELSE learned_criteria END,
                        learned_sample_frequency = CASE WHEN ? != '' THEN ? ELSE learned_sample_frequency END,
                        learned_control_method = CASE WHEN ? != '' THEN ? ELSE learned_control_method END,
                        learned_reaction_plan = CASE WHEN ? != '' THEN ? ELSE learned_reaction_plan END,
                        sample_clause = CASE WHEN ? != '' THEN ? ELSE sample_clause END,
                        sample_quote = CASE WHEN ? != '' THEN ? ELSE sample_quote END,
                        is_active = ?,
                        promoted_at = CASE WHEN ? = 1 AND promoted_at IS NULL THEN datetime('now', 'localtime') ELSE promoted_at END,
                        updated_at = datetime('now', 'localtime')
                    WHERE id = ?
                    """,
                    (
                        final_item.characteristic_name.strip(),
                        dominant_action,
                        app_cnt,
                        rej_cnt,
                        special_cls_val,
                        special_cls_val,
                        spec_val,
                        spec_val,
                        method_val,
                        method_val,
                        criteria_val,
                        criteria_val,
                        sample_freq_val,
                        sample_freq_val,
                        ctrl_method_val,
                        ctrl_method_val,
                        reaction_val,
                        reaction_val,
                        clause_val,
                        clause_val,
                        quote_val,
                        quote_val,
                        now_active,
                        now_active,
                        rule_id,
                    ),
                )
            conn.commit()

            updated_row = conn.execute(
                "SELECT * FROM learned_rules WHERE id = ?", (rule_id,)
            ).fetchone()

        rule_dict = self._row_to_dict(updated_row)

        # 若该规则已升格为自动准入 (approve)，且工程师填写了明确的检查方法，自动补充进 SQLite 量检具词库
        if rule_dict["is_active"] and rule_dict["action"] == "approve":
            self._auto_enrich_method_lexicon(rule_dict["learned_method"], rule_dict["characteristic_name"])

        current_cnt = rule_dict["approve_count"] if action == "approve" else rule_dict["reject_count"]
        action_label = "准入控制计划" if action == "approve" else "驳回剔除"
        if just_promoted:
            msg = (
                f"🧠 自学习规则已自动生效！「{rule_dict['characteristic_name']}」已累计手工{action_label} "
                f"{current_cnt}/{threshold} 次，后续提取将自动按您的规则{'直通放行' if action == 'approve' else '静默剔除'}！"
            )
        elif rule_dict["is_active"]:
            msg = (
                f"🧠 自学习规则已更新：「{rule_dict['characteristic_name']}」（累计手工{action_label} {current_cnt} 次）"
                f"已同步最新修订标准。"
            )
        else:
            msg = (
                f"📊 自学习观察中：「{rule_dict['characteristic_name']}」已累计手工{action_label} "
                f"{current_cnt}/{threshold} 次（满 {threshold} 次系统将自动更改规则）。"
            )

        return {
            "rule": rule_dict,
            "threshold": threshold,
            "current_count": current_cnt,
            "just_promoted": just_promoted,
            "message": msg,
        }

    def _auto_enrich_method_lexicon(self, method_text: str, char_name: str) -> None:
        """当工程师确认准入的检查方法为新工具时，自动扩充进 LexiconDB method 分类"""
        cleaned = (method_text or "").strip()
        if not cleaned or cleaned in ("待确认", "待定", "无", "-", "N/A") or len(cleaned) > 25:
            return
        # 提取括号前的核心量检具名称，如 "洛氏硬度计（按 GB/T 230.1）" -> "洛氏硬度计"
        core_tool = re.split(r"[\(（,，、/]", cleaned)[0].strip()
        if len(core_tool) < 2 or core_tool in ("待确认", "待定"):
            return
        try:
            lex_db = LexiconDB(db_path=self.db_path)
            existing_methods = lex_db.get_grouped_keywords().get("method", [])
            if not any(core_tool.lower() == m.lower() for m in existing_methods):
                lex_db.add_keywords(
                    category="method",
                    raw_keywords=core_tool,
                    description=f"自学习自动收录（源自「{char_name}」人工准入）",
                    lang="zh",
                )
        except Exception:
            pass

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        keys = row.keys()
        return {
            "id": row["id"],
            "pattern_key": row["pattern_key"],
            "plugin_key": row["plugin_key"],
            "standard_category": row["standard_category"],
            "characteristic_name": row["characteristic_name"],
            "action": row["action"],
            "approve_count": row["approve_count"],
            "reject_count": row["reject_count"],
            "learned_special_char_class": row["learned_special_char_class"] if "learned_special_char_class" in keys else "",
            "learned_specification": row["learned_specification"],
            "learned_method": row["learned_method"],
            "learned_criteria": row["learned_criteria"],
            "learned_sample_frequency": row["learned_sample_frequency"] if "learned_sample_frequency" in keys else "",
            "learned_control_method": row["learned_control_method"] if "learned_control_method" in keys else "",
            "learned_reaction_plan": row["learned_reaction_plan"] if "learned_reaction_plan" in keys else "",
            "sample_clause": row["sample_clause"],
            "sample_quote": row["sample_quote"],
            "is_active": bool(row["is_active"]),
            "promoted_at": row["promoted_at"],
            "updated_at": row["updated_at"],
        }

    def list_rules(
        self,
        plugin_key: Optional[str] = None,
        only_active: bool = False,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM learned_rules WHERE 1=1"
        params: List[Any] = []
        if plugin_key:
            query += " AND plugin_key = ?"
            params.append(plugin_key)
        if only_active:
            query += " AND is_active = 1"
        query += " ORDER BY is_active DESC, (approve_count + reject_count) DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_full_learning_payload(self) -> Dict[str, Any]:
        rules = self.list_rules()
        active_rules = [r for r in rules if r["is_active"]]
        pending_rules = [r for r in rules if not r["is_active"]]
        return {
            "threshold": self.get_threshold(),
            "total_rules": len(rules),
            "active_count": len(active_rules),
            "learning_count": len(pending_rules),
            "rules": rules,
        }

    def match_active_rule(
        self,
        standard_category: str,
        characteristic_name: str,
    ) -> Optional[Dict[str, Any]]:
        """
        在后处理门禁中查找是否有匹配当前检查项的已生效自学习规则（严格匹配，禁止宽泛子串包含匹配）：
        1. 优先精确匹配归一化 pattern_key；
        2. 其次在同一标准类别下匹配剥离【工序前缀】后的核心特性名称完全一致项。
        """
        plugin_key, _ = self.resolve_category_pair(standard_category)
        pattern_key = self.build_pattern_key(plugin_key, characteristic_name)
        with self._connect() as conn:
            exact = conn.execute(
                "SELECT * FROM learned_rules WHERE pattern_key = ? AND is_active = 1",
                (pattern_key,),
            ).fetchone()
            if exact:
                return self._row_to_dict(exact)

            candidates = conn.execute(
                "SELECT * FROM learned_rules WHERE plugin_key = ? AND is_active = 1",
                (plugin_key,),
            ).fetchall()

        core_target = self.extract_core_characteristic_name(characteristic_name)
        if core_target:
            for row in candidates:
                core_rule = self.extract_core_characteristic_name(row["characteristic_name"])
                if core_rule and core_rule == core_target:
                    return self._row_to_dict(row)
        return None

    def build_prompt_injection_block(self, plugin_key: str) -> str:
        """
        将当前类别下已生效的自学习规则转化为提示词指令块，由 PromptBuilder 自动追加至提取提示词中。
        """
        active_rules = self.list_rules(plugin_key=plugin_key, only_active=True)
        if not active_rules:
            return ""

        lines = [
            "--- 🧠 系统自学习历史人工复核规则（由质量工程师多次手工确认生成，必须优先遵循） ---"
        ]
        for idx, r in enumerate(active_rules, start=1):
            name = r["characteristic_name"]
            if r["action"] == "approve":
                method_hint = f"，检查方法填「{r['learned_method']}」" if r["learned_method"] else ""
                criteria_hint = f"，判定准则填「{r['learned_criteria']}」" if r["learned_criteria"] else ""
                spec_hint = f"，规范填「{r['learned_specification']}」" if r["learned_specification"] else ""
                cls_hint = f"，特殊特性分类填「{r['learned_special_char_class']}」" if r.get("learned_special_char_class") else ""
                freq_hint = f"，抽样容量与频次填「{r['learned_sample_frequency']}」" if r.get("learned_sample_frequency") else ""
                ctrl_hint = f"，控制方法填「{r['learned_control_method']}」" if r.get("learned_control_method") else ""
                react_hint = f"，反应计划填「{r['learned_reaction_plan']}」" if r.get("learned_reaction_plan") else ""
                lines.append(
                    f"{idx}. 【已学习自动准入项：{name}】遇到涉及「{name}」的条款时（即使原文带有参考/建议表述），"
                    f"质量工程师已确认必须纳入控制计划：请将其提取为正式检查项，强制设置 `\"confidence\": \"高\"`、"
                    f"`\"needs_human_review\": false`{cls_hint}{spec_hint}{method_hint}{criteria_hint}{freq_hint}{ctrl_hint}{react_hint}。"
                )
            else:
                lines.append(
                    f"{idx}. 【已学习自动剔除项：{name}】质量工程师已多次驳回建议性/非受控的「{name}」，若原文无明确强制公差请直接忽略跳过。"
                )
        return "\n".join(lines)

    def update_rule(
        self,
        rule_id: int,
        action: Optional[str] = None,
        is_active: Optional[bool] = None,
        learned_special_char_class: Optional[str] = None,
        learned_specification: Optional[str] = None,
        learned_method: Optional[str] = None,
        learned_criteria: Optional[str] = None,
        learned_sample_frequency: Optional[str] = None,
        learned_control_method: Optional[str] = None,
        learned_reaction_plan: Optional[str] = None,
    ) -> Dict[str, Any]:
        """手动启用/暂停规则，或编辑自学习规则的默认方法、判据与 IATF 16949 扩展字段"""
        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM learned_rules WHERE id = ?", (rule_id,)).fetchone()
            if not existing:
                raise ValueError(f"自学习规则 ID={rule_id} 不存在")

            new_action = action if action in ("approve", "reject") else existing["action"]
            new_active = (1 if is_active else 0) if is_active is not None else existing["is_active"]
            new_cls = learned_special_char_class.strip() if learned_special_char_class is not None else existing["learned_special_char_class"]
            new_spec = learned_specification.strip() if learned_specification is not None else existing["learned_specification"]
            new_method = learned_method.strip() if learned_method is not None else existing["learned_method"]
            new_criteria = learned_criteria.strip() if learned_criteria is not None else existing["learned_criteria"]
            new_freq = learned_sample_frequency.strip() if learned_sample_frequency is not None else existing["learned_sample_frequency"]
            new_ctrl = learned_control_method.strip() if learned_control_method is not None else existing["learned_control_method"]
            new_react = learned_reaction_plan.strip() if learned_reaction_plan is not None else existing["learned_reaction_plan"]

            conn.execute(
                """
                UPDATE learned_rules
                SET action = ?,
                    is_active = ?,
                    learned_special_char_class = ?,
                    learned_specification = ?,
                    learned_method = ?,
                    learned_criteria = ?,
                    learned_sample_frequency = ?,
                    learned_control_method = ?,
                    learned_reaction_plan = ?,
                    promoted_at = CASE WHEN ? = 1 AND promoted_at IS NULL THEN datetime('now', 'localtime') ELSE promoted_at END,
                    updated_at = datetime('now', 'localtime')
                WHERE id = ?
                """,
                (new_action, new_active, new_cls, new_spec, new_method, new_criteria, new_freq, new_ctrl, new_react, new_active, rule_id),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM learned_rules WHERE id = ?", (rule_id,)).fetchone()
        return self._row_to_dict(row)

    def delete_rule(self, rule_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM learned_rules WHERE id = ?", (rule_id,))
            conn.commit()
            return cur.rowcount > 0

    def reset_all_rules(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM learned_rules")
            conn.commit()
