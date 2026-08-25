# Pilot Validation

进入 v0.2 前，`spec-app-consistency-audit` 需要至少一次真实或历史 App 变更 pilot。这个文件定义 pilot 摘要的最小记录格式；它是人工验证记录，不是脚本自动生成的真实结论。

## 目的

- 验证静态审计是否能在真机前发现可修复问题。
- 记录 confirmed、rejected、advisory 的比例和误报原因。
- 确认 rule-pack-only 命中是否正确降级为 advisory。
- 决定是否进入 v0.2，而不是按日程自动扩展。

## 输入要求

pilot 样本必须满足：

- `sample_type` 是 `real_app` 或 `historical_app`。
- 样本包含 PRD、Figma context 和 code implementation 中至少两类项目证据。
- 至少一个 `static_confirmed issue` 同时连接 PRD / Figma / Code 中至少两类 evidence。
- 记录人工确认率、误报原因和真机前可修复问题数量。

## JSON 摘要格式

```json
{
  "sample_type": "historical_app",
  "sample_name": "transfer-failed-state",
  "confirmed_issue_count": 1,
  "rejected_issue_count": 2,
  "advisory_issue_count": 3,
  "manual_confirmation_rate": 0.75,
  "false_positive_reasons": [
    "route name matched but screen intent differed"
  ],
  "pre_runtime_fixable_count": 1,
  "rule_pack_only_hits_downgraded": true,
  "static_confirmed_issues": [
    {
      "id": "APP-AUDIT-PILOT-001",
      "static_confirmed": true,
      "evidence": {
        "prd": [{ "file": "prd.md", "summary": "PRD requires failed transfer copy." }],
        "figma": [{ "file": "figma.json", "summary": "Figma contains failed transfer frame." }],
        "code": [{ "file": "TransferScreen.kt", "summary": "Code has no failed state branch." }]
      }
    }
  ]
}
```

## CLI 接入

`merge-contracts.js` 的 report mode 可通过 `--pilot-validation <json>` 读取这份摘要，并在 `mvp_validation.pilot_validation` 中输出是否满足 v0.2 readiness。

## 边界

- pilot 摘要只能记录人工确认后的事实，不能替代真实审查。
- 缺少 pilot 摘要时，`ready_for_v0_2` 必须为 `false`。
- pilot readiness 只是进入下一阶段的确认点，不是中心化状态机。
