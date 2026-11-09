"""
标准类别识别模块
根据标准标题与正文特征词自动识别标准类别（支持人工指定覆盖），路由到对应插件。
"""

from typing import Dict, List, Optional, Tuple
from ..schemas.models import StandardCategory


class StandardClassifier:
    """标准类别识别器（支持文档级主类别识别与章节块级多插件动态路由）"""

    CATEGORY_MAP = {
        "tolerance": StandardCategory.TOLERANCE,
        "material": StandardCategory.MATERIAL,
        "process": StandardCategory.PROCESS,
        "appearance_performance": StandardCategory.APPEARANCE_PERFORMANCE,
    }

    KEYWORD_WEIGHTS = {
        "tolerance": [
            ("未注公差", 3), ("形位公差", 3), ("几何公差", 3), ("GD&T", 3),
            ("位置度", 2), ("平面度", 2), ("垂直度", 2), ("最大实体", 2),
            ("线性尺寸", 2), ("极限偏差", 2), ("ISO 2768", 3),
        ],
        "material": [
            ("化学成分", 3), ("熔炼分析", 3), ("力学", 2), ("屈服强度", 3),
            ("抗拉强度", 3), ("断后伸长率", 3), ("金相组织", 3), ("晶粒度", 2),
            ("冷弯", 2), ("热连轧", 2), ("GB/T 228", 2),
        ],
        "process": [
            ("工艺规程", 3), ("过程控制参数", 3), ("渗碳", 2), ("淬火", 2),
            ("回火", 2), ("保温时间", 3), ("碳势", 3), ("搅拌器转速", 2),
            ("炉温", 2), ("工序", 2), ("焊接速度", 2),
        ],
        "appearance_performance": [
            ("外观", 3), ("涂装", 2), ("电泳", 2), ("色差", 3),
            ("光泽度", 3), ("流挂", 3), ("缩孔", 3), ("附着力", 3),
            ("百格", 2), ("中性盐雾", 3), ("扩蚀", 2),
        ],
    }

    def score_categories(self, text: str) -> Dict[str, int]:
        """计算文本在四类标准特征词上的加权得分"""
        scores = {k: 0 for k in self.KEYWORD_WEIGHTS}
        for key, kw_list in self.KEYWORD_WEIGHTS.items():
            for kw, weight in kw_list:
                if kw in text:
                    scores[key] += weight
        return scores

    def classify(self, text: str, manual_category: Optional[str] = None) -> Tuple[str, StandardCategory]:
        """
        返回 (plugin_key, StandardCategory)
        若提供 manual_category，则优先使用人工指定类别。
        """
        if manual_category and manual_category != "auto":
            if manual_category in self.CATEGORY_MAP:
                return manual_category, self.CATEGORY_MAP[manual_category]
            for key, cat_enum in self.CATEGORY_MAP.items():
                if manual_category == cat_enum.value:
                    return key, cat_enum
            raise ValueError(f"不支持的标准类别: {manual_category}")

        scores = self.score_categories(text)
        best_key = max(scores, key=scores.get)
        return best_key, self.CATEGORY_MAP[best_key]

    def classify_chunk_plugins(
        self,
        chunk_text: str,
        default_plugin_key: str,
        manual_category: Optional[str] = None,
    ) -> List[str]:
        """
        块级动态路由（Chunk-level Dynamic Routing）：
        1. 若用户显式指定了 manual_category（非 auto），全局强制使用该类别插件；
        2. 对单个章节分块计算四类特征词得分：
           - 若当前块无明显特征词（如总则/通用条款），自动继承文档主类别 default_plugin_key；
           - 若当前块同时高分命中多个类别（次高类别得分 >= 4 且 >= 最高分的 50%），自动合并返回多个插件标识。
        """
        if manual_category and manual_category != "auto":
            key, _ = self.classify(chunk_text, manual_category=manual_category)
            return [key]

        scores = self.score_categories(chunk_text)
        max_score = max(scores.values()) if scores else 0
        if max_score <= 0:
            return [default_plugin_key]

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        selected = [ranked[0][0]]
        for key, sc in ranked[1:]:
            if sc >= 4 and sc >= max_score * 0.5:
                selected.append(key)
        return selected
