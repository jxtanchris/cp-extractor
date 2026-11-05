# GD&T 几何公差特征符号标准参考库 (ASME Y14.5 / ISO 1101)

> 融合自 `CTQ-extraction` 技能规范，用于工程图纸与技术标准中的形位公差 (GD&T Feature Control Frame) 精确解析。

| 公差类别 (Category) | 几何特性名称 (Characteristic) | 英文术语 | 标准符号 (Symbol) | 控制计划特性类别 | 提取与表达规范 (Description) |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **形状公差 (Form)** | 直线度 | Straightness | `⎯` | 尺寸 | 表面素线或圆柱轴线的直线度公差带 |
| **形状公差 (Form)** | 平面度 | Flatness | `⏥` | 尺寸 | 两平行平面之间的表面平整度公差带 |
| **形状公差 (Form)** | 圆度 | Circularity | `⌯` | 尺寸 | 回转体任意正截面的圆形轮廓公差 |
| **形状公差 (Form)** | 圆柱度 | Cylindricity | `⌭` | 尺寸 | 综合控制圆柱面的圆度、直线度与素线平行度 |
| **轮廓度公差 (Profile)** | 线轮廓度 | Profile of a Line | `⌒` | 尺寸 | 任意截面2D理想轮廓线的包络公差带 |
| **轮廓度公差 (Profile)** | 面轮廓度 | Profile of a Surface | `⌓` | 尺寸 | 3D理想曲面的包络公差带（常带基准 A/B/C） |
| **方向公差 (Orientation)** | 垂直度 | Perpendicularity | `⊥` | 尺寸 | 相对于基准平面或轴线成 90° 的方向公差 |
| **方向公差 (Orientation)** | 平行度 | Parallelism | `∥` | 尺寸 | 相对于基准平面或轴线保持平行的方向公差 |
| **方向公差 (Orientation)** | 倾斜度 | Angularity | `∠` | 尺寸 | 相对于基准成指定理论正确角度 (TED) 的公差 |
| **位置公差 (Location)** | 位置度 | Position | `⌖` | 尺寸 | 特征轴线或中心平面相对于基准体系的真实位置公差 |
| **位置公差 (Location)** | 同轴度 / 同心度 | Concentricity | `◎` | 尺寸 | 圆柱轴线或圆心相对于基准轴线的重合度 |
| **位置公差 (Location)** | 对称度 | Symmetry | `≡` | 尺寸 | 中心要素相对于基准中心平面的对称公差 |
| **跳动公差 (Runout)** | 圆跳动 | Circular Runout | `↗` | 尺寸 | 零件绕基准轴线旋转一周时单一测量截面的跳动量 |
| **跳动公差 (Runout)** | 全跳动 | Total Runout | `⌕` | 尺寸 | 零件绕基准轴线连续旋转时整个圆柱/端面的跳动量 |

## CTQ 严格格式化黄金法则 (Strict Formatting Rules)

1. **单边公差原样保留 (Verbatim Unilateral Tolerances)**：
   - 单边公差如 `+0.05/-0.00` 或 `0/-0.03` 必须 100% 原样保留，**严禁**折算或改写为双边公差 `±0.05` 或 `±0.025`。
2. **排除括号参考尺寸 (Exclude Reference Dimensions)**：
   - 凡用圆括号包裹的参考尺寸（如 `(120)`、`(62)`、`(Ø62)`），属于非受控参考信息，一律不纳入控制计划检查项。
3. **特征控制框完整提取 (Feature Control Frame)**：
   - 特性名称应包含几何公差名称与关联基准（如 `位置度 ⌖ (相对于基准 A-B-C)`），规范字段保留完整控制框描述（如 `⌖ Ø0.05 (MMC) A B C`）。
4. **模糊/不可读标注门禁 (Legibility Rule)**：
   - 当图纸或截图局部字迹模糊无法确认公差数值时，严禁猜测，必须将 `confidence` 设为 `"低"` 且 `needs_human_review = true`，并在 `notes` 中注明视图与位置。
