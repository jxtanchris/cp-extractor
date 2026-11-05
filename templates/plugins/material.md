---
category: material
name: 材料标准插件
version: "1.1"
---
重点关注：化学成分范围、抗拉强度、屈服强度、延伸率、硬度、金相组织、盐雾/老化要求。
1. 化学成分表中的每一个元素（如 C、Si、Mn、P、S、Alt）必须逐项独立拆分为单独的检查项，不得合并为笼统的一条“化学成分”。
2. 严格区分"试验方法"和"合格判据"：试验仪器与标准号（如“按 GB/T 228.1 电子万能试验机拉伸”）放入 `inspection_method`，合格数值范围（如 `ReH ≥ 420 MPa`）放入 `acceptance_criteria`。
3. 保留单位与试验条件，如"室温 23±5℃、500h盐雾"。

【Few-Shot 黄金正反例参考】
- ✅ 正例（方法与判据分离）：原文“3.1 按 GB/T 228.1 进行室温拉伸试验，屈服强度 ReH 应不小于 420 MPa” -> `specification`: `"ReH ≥ 420 MPa"`，`inspection_method`: `"室温拉伸试验（按 GB/T 228.1）"`，`acceptance_criteria`: `"ReH ≥ 420 MPa"`。
- ❌ 反例（参考项门禁拦截）：原文“3.3 表面洛氏硬度供内部参考，约 75~88 HRB，不做交货否决判据” -> 提取该项但必须设置 `confidence`: `"低"`, `needs_human_review`: `true`。
