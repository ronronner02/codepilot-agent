# App Consistency Audit Report Format

## 1. Scope & Degraded Modes

说明输入、source freshness、不可审查范围和 degraded modes。no-evidence 不能写成通过。

## 2. Executive Summary

列出 blocker/high/medium/low 数量、证据门禁结果和 runtime verification 建议数量。

## 3. Findings

每个问题必须包含 severity、category、expert、confidence、static_confirmed、contract_status、provenance、evidence、impact、recommendation、related_rule_packs、runtime_verification 和 data_sensitivity。

## 4. Domain Sections

- 产品-设计-代码覆盖矩阵
- 页面路由审查
- KMP + Clean Architecture 审查
- App 工程质量审查
- 组件化 / 模块化 / 复用审查
- 移动端交互审查
- 埋点审查
- 国际化审查
- 行业专项审查

## 5. Evidence Gate

列出 rejected issue 和原因。Rule Pack-only issue 必须降级或拒绝。

## 6. Regression Suggestions

输出候选回归建议，不直接新增测试。

## 7. Writeback Preview

只允许 preview 路径：

- `.spec-first/app-audit/runs/<run-id>/writeback-preview/repo-profile.patch.yaml`
- `.spec-first/app-audit/runs/<run-id>/writeback-preview/suggested-standards.md`

禁止自动修改 durable repo-profile 或 standards。
