"""
量检具、物理单位与判定准则校验词库 SQLite 存储模块（支持中/英/日/德多语言对照与同义词扩展）
支持通过 Web 界面或 API 动态增删改查及恢复出厂默认，实时作用于 QualityGateValidator 后处理门禁。
"""

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_DB_PATH = ROOT_DIR / "data" / "lexicon.db"

LEXICON_CATEGORIES: Dict[str, Dict[str, str]] = {
    "method": {
        "title": "🔧 量检具与试验方法词库",
        "description": "用于识别中/英/日/德多语种检查方法，并拦截将量检具或试验设备误填入「判定准则」的语义混淆错误。",
    },
    "unit": {
        "title": "📏 物理单位与公差符号词库",
        "description": "用于校验含数值的「规范/公差」字段是否具备明确的物理单位、多语言单位表述或 GD&T 几何特征符号。",
    },
    "criteria": {
        "title": "⚖️ 合格判据特征词库",
        "description": "用于标识「判定准则」中的中/英/日/德合法边界符号、合格等级或零缺陷状态描述。",
    },
}

SUPPORTED_LANGUAGES: Dict[str, str] = {
    "zh": "🇨🇳 中文 (ZH)",
    "en": "🇺🇸 英文 (EN)",
    "ja": "🇯🇵 日文 (JA)",
    "de": "🇩🇪 德文 (DE)",
    "all": "🔣 通用符号 (ALL)",
}

# 四元组格式: (keyword, description, lang, aliases)
DEFAULT_LEXICON: Dict[str, List[tuple[str, str, str, str]]] = {
    "method": [
        ("试验机", "通用力学/材料试验设备", "zh", "Testing machine, Universal testing machine, 試験機, Prüfmaschine"),
        ("卡尺", "通用线性尺寸量具", "zh", "Caliper, Vernier caliper, Digital caliper, ノギス, Messschieber"),
        ("千分尺", "精密外径/厚度量具", "zh", "Micrometer, マイクロメータ, Bügelmessschraube"),
        ("投影仪", "光学轮廓/尺寸测量设备", "zh", "Profile projector, Optical comparator, 投影機, Messprojektor"),
        ("显微镜", "金相/微观缺陷观测设备", "zh", "Microscope, 顕微鏡, Mikroskop"),
        ("三坐标", "CMM 空间尺寸与形位公差测量机", "zh", "CMM, Coordinate measuring machine, 三次元測定機, KMG"),
        ("目视", "外观检查方式", "zh", "Visual, Visual inspection, 目視, 外観検査, Sichtprüfung"),
        ("盐雾箱", "耐腐蚀性能试验设备", "zh", "Salt spray chamber, Salt spray test, 塩水噴霧試験, 塩水噴霧, Salzsprühnebelprüfung"),
        ("探伤仪", "无损检测设备", "zh", "Flaw detector, NDT, Ultrasonic testing, 探傷器, 磁粉探傷, 超音波探傷, ZfP"),
        ("硬度计", "洛氏/维氏/布氏硬度测试设备", "zh", "Hardness tester, Durometer, 硬さ試験機, 硬度計, Härteprüfgerät"),
        ("色差仪", "外观颜色 ΔE 测量仪器", "zh", "Colorimeter, Spectrophotometer, 色差計, 分光測色計, Farbmessgerät"),
        ("粗糙度仪", "表面粗糙度 Ra/Rz 测量仪", "zh", "Roughness tester, Profilometer, 表面粗さ測定機, 粗さ計, Rauheitsmessgerät"),
        ("拉伸试验", "材料力学拉伸测试方法", "zh", "Tensile test, 引張試験, Zugversuch"),
        ("冲击试验", "材料韧性冲击测试方法", "zh", "Impact test, Charpy test, 衝撃試験, シャルピー衝撃試験, Kerbschlagbiegeversuch"),
        ("金相检验", "材料显微组织评定方法", "zh", "Metallographic test, Microstructure, 金属組織試験, 顕微鏡組織試験, Gefügeprüfung"),
        ("塞规", "孔径通止检验量具", "zh", "Plug gauge, Go/No-Go gauge, 栓ゲージ, 限界ゲージ, Lehrdorn"),
        ("气动量仪", "精密内孔尺寸测量仪", "zh", "Air gauge, Pneumatic gauge, エアーマイクロメータ, 空気マイクロメータ"),
        ("百格刀", "涂层附着力划格试验工具", "zh", "Cross-cut tester, Adhesion test, クロスカット試験, 碁盤目試験, Gitterschnitt"),
        ("测厚仪", "涂层/镀层膜厚测量仪器", "zh", "Coating thickness gauge, 膜厚計, 塗膜厚計, Schichtdickenmessgerät"),
        ("光谱仪", "化学成分直读光谱分析设备", "zh", "Spectrometer, OES, ICP-OES, 発光分光分析装置, 分光分析"),
        ("pH计", "溶液酸碱度测量仪器", "zh", "pH meter, 酸度计, pH計, ペーハー計"),
        ("比重计", "溶液密度/浓度滴定与测量", "zh", "Hydrometer, Density meter, 密度计, 盐度计, 比重計, 塩分計"),
        # --- 预置 🇺🇸 English (ISO / ASTM / ASME) 常用量检具与试验方法 ---
        ("CMM", "Coordinate Measuring Machine (三坐标测量机)", "en", "Coordinate measuring machine, 3D measuring machine"),
        ("Vernier caliper", "游标/数显卡尺 (ISO/ASTM)", "en", "Caliper, Digital caliper"),
        ("Micrometer", "外径/内径千分尺", "en", "Outside micrometer, Inside micrometer"),
        ("Visual inspection", "目视外观检查", "en", "Visual check, Visual examination"),
        ("Tensile test", "拉伸力学性能试验 (ISO 6892 / ASTM E8)", "en", "Tensile testing machine, Universal testing machine"),
        ("Salt spray test", "中性/铜加速盐雾试验 (ISO 9227 / ASTM B117)", "en", "Salt spray chamber, NSS test, CASS test"),
        ("Cross-cut test", "涂层百格附着力试验 (ISO 2409 / ASTM D3359)", "en", "Cross-hatch cutter, Adhesion test"),
        ("Coating thickness gauge", "涂层干膜测厚仪", "en", "Eddy current gauge, Magnetic thickness gauge"),
        ("Hardness tester", "洛氏/维氏/布氏硬度计", "en", "Rockwell hardness tester, Vickers hardness tester, Brinell hardness"),
        ("Spectrometer", "直读光谱/ICP化学成分分析仪", "en", "OES, ICP-OES, Optical emission spectrometer"),
        # --- 预置 🇯🇵 日本語 (JIS 规格标准) 常用量检具与试验方法 ---
        ("ノギス", "游标卡尺/数显卡尺 (JIS B 7507)", "ja", "デジタルノギス, キャリパ"),
        ("マイクロメータ", "外径千分尺 (JIS B 7502)", "ja", "外側マイクロメータ, 内側マイクロメータ"),
        ("三次元測定機", "三坐标测量机 (CMM)", "ja", "三次元座標測定機"),
        ("目視", "目视外观检查 (JIS 外観検査)", "ja", "目視検査, 外観検査, 肉眼観察"),
        ("塩水噴霧試験", "盐水喷雾耐腐蚀试验 (JIS Z 2371 / JIS H 8502)", "ja", "塩水噴霧試験機, 中性塩水噴霧, キャス試験"),
        ("引張試験", "金属材料拉伸试验 (JIS Z 2241)", "ja", "引張試験機, 万能試験機"),
        ("硬さ試験機", "硬度试验机 (洛氏/维氏/布氏)", "ja", "ロックウェル硬さ試験, ビッカース硬さ試験, ブリネル硬さ"),
        ("碁盤目試験", "涂膜百格附着力试验 (JIS K 5600)", "ja", "クロスカット試験, 付着性試験"),
        ("膜厚計", "涂镀层膜厚测量仪", "ja", "電磁膜厚計, 渦電流膜厚計, 塗膜厚計"),
        ("表面粗さ測定機", "表面粗糙度仪 (JIS B 0601)", "ja", "粗さ計, 面粗度計"),
        ("栓ゲージ", "孔径通止塞规 / 极限量规", "ja", "限界ゲージ, プラグゲージ, ねじゲージ"),
        # --- 预置 🇩🇪 Deutsch (DIN / VDA 德国汽车标准) 常用量检具与试验方法 ---
        ("Messschieber", "游标卡尺 (DIN 862)", "de", "Taschenmessschieber, Digitaler Messschieber"),
        ("Bügelmessschraube", "外径千分尺 (DIN 863)", "de", "Mikrometer"),
        ("KMG", "Koordinatenmessgerät (三坐标测量机)", "de", "Koordinatenmessmaschine"),
        ("Sichtprüfung", "目视外观检查", "de", "Visuelle Prüfung"),
        ("Zugversuch", "金属材料拉伸试验 (DIN EN ISO 6892)", "de", "Zugprüfmaschine"),
        ("Salzsprühnebelprüfung", "盐雾耐腐蚀试验 (DIN EN ISO 9227)", "de", "Salzsprühtest"),
        ("Gitterschnitt", "百格附着力试验 (DIN EN ISO 2409)", "de", "Gitterschnittprüfung"),
        ("Schichtdickenmessgerät", "涂层测厚仪", "de", "Schichtdickenmessung"),
    ],
    "unit": [
        ("mm", "毫米（长度单位）", "all", "毫米, millimeter, ミリ"),
        ("cm", "厘米（长度单位）", "all", "厘米, centimeter, センチ"),
        ("m", "米（长度单位）", "all", "meter"),
        ("μm", "微米（膜厚/粗糙度单位）", "all", "um, micron, 微米, マイクロメートル"),
        ("nm", "纳米", "all", "nanometer, 纳米"),
        ("%", "百分比（化学成分/延伸率/浓度）", "all", "％, wt%, mass%, 質量%, 质量分数, 質量"),
        ("MPa", "兆帕（抗拉/屈服强度单位）", "all", "N/mm², N/mm2, 兆帕"),
        ("GPa", "吉帕（弹性模量单位）", "all", "吉帕"),
        ("N", "牛顿（力值单位）", "all", "kN, 牛顿, ニュートン"),
        ("N·m", "牛米（扭矩单位）", "all", "Nm, N.m, 牛米"),
        ("kg", "千克（质量单位）", "all", "g, mg, 千克, 公斤, グラム"),
        ("℃", "摄氏度（温度单位）", "all", "°C, degC, 摄氏度, 度"),
        ("°", "角度单位（度）", "all", "deg, degree, 度"),
        ("h", "小时（保温/盐雾时间单位）", "all", "hr, hrs, hours, 小时, 時間"),
        ("min", "分钟（工艺时间单位）", "all", "mins, minutes, 分钟, 分"),
        ("s", "秒（时间单位）", "all", "sec, 秒"),
        ("r/min", "转速单位（转/分）", "all", "rpm, RPM, min-1, 转/分"),
        ("GU", "光泽单位（Gloss Unit）", "all", "光泽单位, グロス"),
        ("ppm", "百万分率（固形物/杂质浓度单位）", "all", "PPm, mg/L, mg/kg"),
        ("lx", "勒克斯（光照度单位）", "all", "lux, ルクス"),
        ("±", "双边对称公差符号", "all", "+/-, ＋－"),
        ("HRC", "洛氏硬度 C 标尺", "all", "洛氏硬度"),
        ("HRB", "洛氏硬度 B 标尺", "all", ""),
        ("HV", "维氏硬度", "all", "维氏硬度, ビッカース硬さ"),
        ("HB", "布氏硬度", "all", "HBW, 布氏硬度, ブリネル硬さ"),
        ("Ra", "算术平均表面粗糙度", "all", "表面粗糙度, 算術平均粗さ"),
        ("Rz", "微观不平度十点高度", "all", "最大高さ粗さ"),
        ("Ø", "直径符号", "all", "φ, Φ, DIA"),
        ("⎯", "GD&T 直线度 (Straightness)", "all", "直线度, 真直度, Straightness"),
        ("⏥", "GD&T 平面度 (Flatness)", "all", "平面度, Flatness"),
        ("⌯", "GD&T 圆度 (Circularity)", "all", "圆度, 真円度, Circularity, Roundness"),
        ("⌭", "GD&T 圆柱度 (Cylindricity)", "all", "圆柱度, 円筒度, Cylindricity"),
        ("⌒", "GD&T 线轮廓度 (Profile of a line)", "all", "线轮廓度, 線の輪郭度"),
        ("⌓", "GD&T 面轮廓度 (Profile of a surface)", "all", "面轮廓度, 面の輪郭度"),
        ("⊥", "GD&T 垂直度 (Perpendicularity)", "all", "垂直度, 直角度, Perpendicularity"),
        ("∥", "GD&T 平行度 (Parallelism)", "all", "平行度, Parallelism"),
        ("∠", "GD&T 倾斜度 (Angularity)", "all", "倾斜度, 傾斜度, Angularity"),
        ("⌖", "GD&T 位置度 (Position)", "all", "位置度, Position"),
        ("◎", "GD&T 同轴/同心度 (Concentricity)", "all", "同轴度, 同心度, Concentricity, Coaxiality"),
        ("≡", "GD&T 对称度 (Symmetry)", "all", "对称度, 対称度, Symmetry"),
        ("↗", "GD&T 圆跳动 (Circular Runout)", "all", "圆跳动, 円周振れ, Circular runout"),
        ("⌕", "GD&T 全跳动 (Total Runout)", "all", "全跳动, 全振れ, Total runout"),
    ],
    "criteria": [
        ("≥", "不小于/大于等于", "all", ">=, 不小于, 不少于, 不低于, 以上, Min., Minimum, Mindestens"),
        ("≤", "不大于/小于等于", "all", "<=, 不大于, 不超过, 不高于, 以下, 以内, Max., Maximum, Höchstens"),
        (">", "大于", "all", "大于, 超过, Greater than, 超える"),
        ("<", "小于", "all", "小于, 低于, Less than, 未満"),
        ("±", "正负公差范围", "all", "+/-, 范围内, 範囲内"),
        ("%", "百分比判据", "all", "％, wt%, 質量%"),
        ("MPa", "强度判据", "all", "N/mm²"),
        ("mm", "尺寸判据", "all", "μm, um"),
        ("无裂纹", "外观/冷弯定性合格判据", "zh", "No crack, Crack-free, Without cracks, 割れなきこと, 亀裂なきこと, 割れなし, Rissfrei"),
        ("不得有", "零缺陷否定判据", "zh", "Free from, Shall not have, None, 无起泡, 无生锈, なきこと, ないこと, 有害な欠点なきこと"),
        ("级", "等级合格判据（如 0级、1级、9.0级）", "zh", "Grade, Class, Level, Rating, 級, クラス, 相当品, Stufe, Klasse"),
        ("符合", "符合性判据", "zh", "Conform to, Comply with, Meet, 適合, 準拠, 規定による, 判定基準, Entsprechen"),
        ("Ra", "粗糙度判据", "all", "Rz"),
        ("HRC", "洛氏硬度判据", "all", "HRB, HV, HB"),
        ("N·m", "扭矩判据", "all", "Nm"),
        ("Ø", "直径公差带判据", "all", "φ, Φ"),
        ("⎯", "GD&T 直线度判据", "all", "⏥, ⌯, ⌭, ⌒, ⌓, ⊥, ∥, ∠, ⌖, ◎, ≡, ↗, ⌕"),
        # --- 预置 🇺🇸 English (ISO / ASTM / ASME) 常用合格判据特征词 ---
        ("No crack", "无裂纹/无开裂合格判据", "en", "Crack-free, Without cracks, Free of cracks"),
        ("Free from", "零缺陷外观合格判据 (如 Free from burrs/blisters)", "en", "Free of, Shall not have, No visible defects"),
        ("Grade", "等级合格判据 (如 Grade 0, Grade 1)", "en", "Class, Level, Rating"),
        ("Min.", "最小值/下限合格判据", "en", "Minimum, At least, Not less than"),
        ("Max.", "最大值/上限合格判据", "en", "Maximum, At most, Not more than"),
        ("Conforms to", "符合标准规范判据", "en", "Complies with, Meets, In accordance with"),
        # --- 预置 🇯🇵 日本語 (JIS 规格标准) 常用合格判据特征词 ---
        ("割れなきこと", "无裂纹合格判据 (JIS 冷弯/外观)", "ja", "亀裂なきこと, 割れがないこと, クラックなきこと"),
        ("なきこと", "零缺陷否定判据 (如 ふくれ、錆なきこと)", "ja", "ないこと, 有害な欠点なきこと, 異常なきこと"),
        ("以上", "下限合格判据 (≥)", "ja", "を下回らないこと"),
        ("以下", "上限合格判据 (≤)", "ja", "以内, を超えないこと"),
        ("級", "等级合格判据 (如 1級相当品、0級)", "ja", "クラス, 相当品, 等級"),
        ("適合", "符合标准规范判据 (JIS 規定に適合)", "ja", "準拠, 判定基準, 規定による"),
        # --- 预置 🇩🇪 Deutsch (DIN / VDA) 常用合格判据特征词 ---
        ("Rissfrei", "无裂纹合格判据 (DIN/VDA)", "de", "Ohne Risse, Keine Risse"),
        ("Mindestens", "最小值/不小于判据 (Min.)", "de", "Min."),
        ("Höchstens", "最大值/不大于判据 (Max.)", "de", "Max."),
        ("Klasse", "等级/级别合格判据", "de", "Stufe, Gütegrad"),
    ],
}


class LexiconDB:
    """基于 SQLite 的量检具、物理单位与判定准则多语言词库管理器"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """初始化 SQLite 表结构，自动升级旧版表结构并补齐多语言对照数据"""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS validation_lexicon (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    lang TEXT DEFAULT 'zh',
                    aliases TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    is_builtin INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(category, keyword)
                )
                """
            )
            # 兼容升级：若旧表中尚未包含 lang 或 aliases 字段，自动执行 ALTER TABLE 并回填默认多语言别名
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(validation_lexicon)").fetchall()
            }
            migrated = False
            if "lang" not in cols:
                conn.execute("ALTER TABLE validation_lexicon ADD COLUMN lang TEXT DEFAULT 'zh'")
                migrated = True
            if "aliases" not in cols:
                conn.execute("ALTER TABLE validation_lexicon ADD COLUMN aliases TEXT DEFAULT ''")
                migrated = True

            cursor = conn.execute("SELECT COUNT(*) AS cnt FROM validation_lexicon")
            count = cursor.fetchone()["cnt"]
            default_total = sum(len(v) for v in DEFAULT_LEXICON.values())
            if count == 0:
                self._seed_defaults(conn)
            elif migrated or count < default_total:
                self._backfill_multilingual_defaults(conn)
            conn.commit()

    def _seed_defaults(self, conn: sqlite3.Connection, target_category: Optional[str] = None) -> None:
        categories = [target_category] if target_category in DEFAULT_LEXICON else list(DEFAULT_LEXICON.keys())
        for cat in categories:
            rows = [
                (cat, kw, lang, aliases, desc, 1)
                for kw, desc, lang, aliases in DEFAULT_LEXICON[cat]
            ]
            conn.executemany(
                """
                INSERT OR IGNORE INTO validation_lexicon (category, keyword, lang, aliases, description, is_builtin)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _backfill_multilingual_defaults(self, conn: sqlite3.Connection) -> None:
        """对旧版升级上来的内置词条自动补充默认 lang 与多语言 aliases"""
        for cat, items in DEFAULT_LEXICON.items():
            for kw, desc, lang, aliases in items:
                conn.execute(
                    """
                    UPDATE validation_lexicon
                    SET lang = ?, aliases = CASE WHEN aliases = '' OR aliases IS NULL THEN ? ELSE aliases END
                    WHERE category = ? AND keyword = ?
                    """,
                    (lang, aliases, cat, kw),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO validation_lexicon (category, keyword, lang, aliases, description, is_builtin)
                    VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (cat, kw, lang, aliases, desc),
                )

    @staticmethod
    def _split_tokens(raw_text: str) -> List[str]:
        return [t.strip() for t in re.split(r"[,，、\n\r]+", raw_text or "") if t.strip()]

    def list_keywords(
        self,
        category: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询词条列表（支持按分类和语言筛选）"""
        with self._connect() as conn:
            query = """
                SELECT id, category, keyword, lang, aliases, description, is_builtin, created_at
                FROM validation_lexicon
                WHERE 1=1
            """
            params: List[Any] = []
            if category:
                if category not in LEXICON_CATEGORIES:
                    raise ValueError(f"不支持的词库分类: {category}")
                query += " AND category = ?"
                params.append(category)
            if lang and lang != "all_filter":
                query += " AND lang = ?"
                params.append(lang)
            query += " ORDER BY id ASC"
            rows = conn.execute(query, params).fetchall()

        return [
            {
                "id": r["id"],
                "category": r["category"],
                "keyword": r["keyword"],
                "lang": r["lang"] or "zh",
                "aliases": r["aliases"] or "",
                "alias_list": self._split_tokens(r["aliases"] or ""),
                "description": r["description"] or "",
                "is_builtin": bool(r["is_builtin"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def get_grouped_keywords(self) -> Dict[str, List[str]]:
        """
        按分类返回展开了多语言同义词 (`keyword` + `aliases`) 的完整关键词列表，
        供 QualityGateValidator 在校验中/英/日/德标准文档时实时匹配。
        """
        grouped: Dict[str, List[str]] = {k: [] for k in LEXICON_CATEGORIES}
        seen: Dict[str, set] = {k: set() for k in LEXICON_CATEGORIES}

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT category, keyword, aliases FROM validation_lexicon ORDER BY id ASC"
            ).fetchall()

        for r in rows:
            cat = r["category"]
            if cat not in grouped:
                continue
            candidates = [r["keyword"]] + self._split_tokens(r["aliases"] or "")
            for token in candidates:
                if token and token not in seen[cat]:
                    seen[cat].add(token)
                    grouped[cat].append(token)
        return grouped

    def get_full_lexicon_payload(self) -> Dict[str, Any]:
        """返回供前端渲染的完整多语言词库信息（含分类元数据、语言列表与词条列表）"""
        all_items = self.list_keywords()
        by_cat: Dict[str, List[Dict[str, Any]]] = {k: [] for k in LEXICON_CATEGORIES}
        total_expanded_terms = 0
        for item in all_items:
            total_expanded_terms += 1 + len(item["alias_list"])
            if item["category"] in by_cat:
                by_cat[item["category"]].append(item)

        categories_info = []
        for key, meta in LEXICON_CATEGORIES.items():
            cat_items = by_cat[key]
            expanded_cnt = sum(1 + len(i["alias_list"]) for i in cat_items)
            categories_info.append({
                "key": key,
                "title": meta["title"],
                "description": meta["description"],
                "count": len(cat_items),
                "expanded_count": expanded_cnt,
                "items": cat_items,
            })
        return {
            "db_path": str(self.db_path.relative_to(ROOT_DIR)) if self.db_path.is_relative_to(ROOT_DIR) else str(self.db_path),
            "total_count": len(all_items),
            "total_expanded_terms": total_expanded_terms,
            "supported_languages": SUPPORTED_LANGUAGES,
            "categories": categories_info,
        }

    def add_keywords(
        self,
        category: str,
        raw_keyword_input: str,
        description: str = "",
        lang: str = "zh",
        aliases: str = "",
    ) -> List[Dict[str, Any]]:
        """
        新增一个或多个词条（支持以逗号、顿号或换行批量分隔输入，并附带语种标识与多语言同义词对照）
        """
        if category not in LEXICON_CATEGORIES:
            raise ValueError(f"不支持的词库分类: {category}")
        lang_code = (lang or "zh").strip().lower()
        if lang_code not in SUPPORTED_LANGUAGES:
            lang_code = "zh"

        tokens = self._split_tokens(raw_keyword_input)
        if not tokens:
            raise ValueError("词条内容不能为空")

        desc = (description or "").strip()
        clean_aliases = ", ".join(self._split_tokens(aliases))
        added_or_updated: List[Dict[str, Any]] = []

        with self._connect() as conn:
            for kw in tokens:
                conn.execute(
                    """
                    INSERT INTO validation_lexicon (category, keyword, lang, aliases, description, is_builtin)
                    VALUES (?, ?, ?, ?, ?, 0)
                    ON CONFLICT(category, keyword) DO UPDATE SET
                        lang = excluded.lang,
                        aliases = CASE WHEN excluded.aliases != '' THEN excluded.aliases ELSE validation_lexicon.aliases END,
                        description = CASE WHEN excluded.description != '' THEN excluded.description ELSE validation_lexicon.description END
                    """,
                    (category, kw, lang_code, clean_aliases, desc),
                )
                row = conn.execute(
                    """
                    SELECT id, category, keyword, lang, aliases, description, is_builtin, created_at
                    FROM validation_lexicon
                    WHERE category = ? AND keyword = ?
                    """,
                    (category, kw),
                ).fetchone()
                if row:
                    added_or_updated.append({
                        "id": row["id"],
                        "category": row["category"],
                        "keyword": row["keyword"],
                        "lang": row["lang"] or "zh",
                        "aliases": row["aliases"] or "",
                        "alias_list": self._split_tokens(row["aliases"] or ""),
                        "description": row["description"] or "",
                        "is_builtin": bool(row["is_builtin"]),
                        "created_at": row["created_at"],
                    })
            conn.commit()
        return added_or_updated

    def update_keyword(
        self,
        item_id: int,
        keyword: str,
        description: str = "",
        lang: Optional[str] = None,
        aliases: Optional[str] = None,
    ) -> Dict[str, Any]:
        """修改指定 ID 的词条名称、语种、多语言同义词或备注说明"""
        kw = (keyword or "").strip()
        if not kw:
            raise ValueError("词条内容不能为空")
        desc = (description or "").strip()

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, category, lang, aliases FROM validation_lexicon WHERE id = ?",
                (item_id,),
            ).fetchone()
            if not existing:
                raise ValueError(f"未找到 ID={item_id} 的词条")

            final_lang = (lang or existing["lang"] or "zh").strip().lower()
            if final_lang not in SUPPORTED_LANGUAGES:
                final_lang = "zh"
            final_aliases = (
                ", ".join(self._split_tokens(aliases))
                if aliases is not None
                else (existing["aliases"] or "")
            )

            try:
                conn.execute(
                    """
                    UPDATE validation_lexicon
                    SET keyword = ?, lang = ?, aliases = ?, description = ?
                    WHERE id = ?
                    """,
                    (kw, final_lang, final_aliases, desc, item_id),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"当前分类下已存在相同的词条: '{kw}'")

            row = conn.execute(
                """
                SELECT id, category, keyword, lang, aliases, description, is_builtin, created_at
                FROM validation_lexicon
                WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
            return {
                "id": row["id"],
                "category": row["category"],
                "keyword": row["keyword"],
                "lang": row["lang"] or "zh",
                "aliases": row["aliases"] or "",
                "alias_list": self._split_tokens(row["aliases"] or ""),
                "description": row["description"] or "",
                "is_builtin": bool(row["is_builtin"]),
                "created_at": row["created_at"],
            }

    def delete_keyword(self, item_id: int) -> bool:
        """根据 ID 删除指定词条"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM validation_lexicon WHERE id = ?", (item_id,))
            conn.commit()
            return cursor.rowcount > 0

    def reset_to_defaults(self, category: Optional[str] = None) -> Dict[str, Any]:
        """将指定分类（或全部）恢复为系统出厂默认多语言词库"""
        with self._connect() as conn:
            if category and category != "all":
                if category not in LEXICON_CATEGORIES:
                    raise ValueError(f"不支持的词库分类: {category}")
                conn.execute("DELETE FROM validation_lexicon WHERE category = ?", (category,))
                self._seed_defaults(conn, target_category=category)
            else:
                conn.execute("DELETE FROM validation_lexicon")
                self._seed_defaults(conn)
            conn.commit()
        return self.get_full_lexicon_payload()
