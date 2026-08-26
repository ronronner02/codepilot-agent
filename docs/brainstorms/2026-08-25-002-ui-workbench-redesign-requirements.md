---
spec_id: 2026-08-25-002-ui-workbench-redesign
artifact_kind: prd-requirements
target_surface: H5/PC
status: superseded
superseded_by: 2026-08-25-003-code-intelligence-workbench
evidence_grade: mixed
source_authority: mixed
readiness_authority: engineering-owned
created: 2026-08-25
source_inputs:
  - docs/plans/2026-08-23-001-feat-codepilot-agent-plan.md
  - docs/2026-08-25-verification-backlog.html
  - frontend/src/App.tsx
  - frontend/src/styles.css
  - frontend/src/test/App.test.tsx
write_mode: route-out
can_enter_spec_plan: no
clarification_evidence: asked-owner
preflight_sweep_closure: closed
next_owner_question: none
---

# CodePilot 工作台界面改版 增量需求文档

## PRD 元数据

| 项 | 内容 |
| --- | --- |
| 需求名称 | CodePilot 工作台界面改版 |
| 需求编号 / spec_id | 2026-08-25-002-ui-workbench-redesign |
| 业务域 | 前端界面层（U13 React 前端） |
| 目标 surface | H5/PC（桌面为主的单页 Web 应用） |
| 目标地区 / 市场 / tenant | 不涉及 |
| 目标用户 / 客户类型 | 项目作者本人与作品评审者（实习项目演示场景） |
| 是否触及付费、资金或交易 | 否 |
| 是否触及个人信息或敏感数据 | 否 |
| 是否需要外部规则或专业意见 | 否 |
| 相关文档 | `docs/plans/2026-08-23-001-feat-codepilot-agent-plan.md`（R22/R23、U13 状态矩阵与可访问交互要求）；配色参考图（用户提供，见 Design Source Coverage） |

## Summary

为 CodePilot 的单页界面替换视觉与布局外壳：把当前「单列纵向堆叠」改为「左侧锚点导航 + 顶部仓库标识条 + 主内容区」的工作台形态，配色以 `#303d67` 深蓝为基调派生，并在结果区新增一条由现有 API 字段派生的分析结果统计条。界面承载的能力集合不变，仍是 R22 的五件事（提交地址、看进度、读报告、追问、看评审发现）。

## Problem Frame

当前界面是能用但观感粗糙的单列文档流：五个能力区块按纵向顺序平铺，没有全局导航，分析完成后报告、评审、问答三段内容连续堆叠，读者要靠滚动条找位置。作为实习作品，界面是评审者的第一接触面，「能跑」与「看起来像个产品」之间的差距会直接折损前面 15 个实现单元的说服力。

不做的代价：后端 693 个测试、可追溯报告、缓存加速比 6094× 这些实质工作，被一个粗糙外壳挡在评审者的第一印象之后。

## Change Delta

| 变化类型 | 内容 | 涉及现有能力 | 用户/数据/运营影响 | 证据 tag |
| --- | --- | --- | --- | --- |
| keep | 五个能力区块的行为、状态机、SSE 生命周期、失败四分类、焦点与读屏契约全部不动 | `App.tsx` 的 `Phase` 状态机与五个组件的交互逻辑 | 无行为变化 | confirmed-source: `frontend/src/App.tsx:22-204` |
| keep | 后端 API 契约、请求次数、字段集合完全不动 | `backend/api/routes.py`、`frontend/src/api/types.ts` | 无 | confirmed-source: `frontend/src/api/types.ts:1-144` |
| keep | 类名 `.progress__detail` 保留（两个测试用它做选择器） | `ProgressPanel` | 无 | confirmed-source: `frontend/src/test/App.test.tsx:247,302` |
| replace | 视觉层全量替换：配色、字体尺度、间距节奏、圆角、边框、投影 | `frontend/src/styles.css`（549 行全量重写） | 观感变化，无行为变化 | confirmed-source: `frontend/src/styles.css` |
| replace | 布局外壳：单列纵向流 → 左侧栏 + 顶部条 + 主内容区网格 | `App.tsx` 根节点结构 | 桌面端信息定位效率提升 | user-stated（D1 决定，含 ASCII 预览） |
| extend | 新增侧栏锚点导航（概览/报告/评审/问答），点击滚动并高亮当前区块 | 新增外壳，不改各区块内部 | 长页面内定位 | user-stated（D1） |
| extend | 新增顶部仓库标识条（仓库名 + commit 短 SHA + 阶段徽标） | 复用 `AnalysisResult.repo`、`Report.commit_sha`、`Stage` | 当前上下文常驻可见 | confirmed-source: `frontend/src/api/types.ts:74-127` |
| extend | 新增结果侧统计条：4 张统计卡 + 严重度分布条 | 全部由 `AnalysisResult` 现有字段派生 | 报告质量与覆盖度一眼可见 | user-stated（D1）+ 字段来源见 R-09 |
| remove | 无 | — | — | — |
| unknown | 无 | — | — | — |

历史逻辑说明：本次不沿用任何既有视觉规则。`styles.css` 整份替换，原文件不保留兜底样式；保留项只有上表 keep 行明确列出的三项（行为契约、API 契约、`.progress__detail` 类名）。

## Current System Snapshot

| 现状项 | 当前行为 | 证据 tag |
| --- | --- | --- |
| 布局 | 单列纵向堆叠，根节点 `.app` 下依次为 header / RepoInput / failure / ProgressPanel / results；无侧栏、无顶部条、无路由 | confirmed-source: `frontend/src/App.tsx:141-203` |
| 样式 | 手写 CSS 549 行，浅色底，无设计令牌体系，零 UI 依赖（`package.json` 仅 react + react-dom） | confirmed-source: `frontend/src/styles.css`、`frontend/package.json:14-17` |
| 完成态区块共存 | 分析成功后 `架构报告`、`代码评审`、`代码问答` 三个 `h2` 同时挂载在 `.results` 内 | confirmed-source: `frontend/src/App.tsx:177-201` |
| 焦点契约 | 提交后焦点移至 `分析进度` 标题，完成后移至 `架构报告` 标题，两者均 `tabIndex={-1}` | confirmed-source: `ProgressPanel.tsx:26-28`、`ReportView.tsx:31-33` |
| 读屏契约 | 进度区 `role="status"`（隐含 polite），断连与错误用 `role="alert"`，折叠区用原生 details 元素 | confirmed-source: `ProgressPanel.tsx:38,70`、`RepoInput.tsx:65`、`QaPanel.tsx:86` |
| 测试基线 | 前端 40 个测试通过、`tsc --noEmit` 无错误、后端 693 通过 1 跳过 | confirmed-source: 本次实跑 `npm test`、`npm run typecheck`、`pytest -q` |
| 可用统计字段 | `review.findings[].severity/category`、`review.outcomes[].hit_count/status`、`index.chunk_count`、`report.missing.unparsed_files/skipped_modules`、`report.unsupported_claims`、`module_failures` | confirmed-source: `frontend/src/api/types.ts:85-127` |
| 不可用统计字段 | 无耗时、无 token 成本、无时间序列、无 per-module 进度快照 | confirmed-source: `frontend/src/api/types.ts` 全文 |

## Requirements

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-01 | 打开界面时 | 系统 | 应以「左侧导航栏 + 顶部标识条 + 主内容区」三区网格渲染工作台外壳 | 桌面视口下左侧栏常驻、顶部条常驻、内容区独立滚动 | D1 预览 |
| R-02 | 视口宽度小于 960px 时 | 系统 | 应将左侧栏转为顶部横向导航，主内容区占满宽度 | 窄视口下导航横向排列，内容纵向堆叠 | U13 响应式要求 |
| R-03 | 用户点击侧栏导航项时 | 用户 | 系统应平滑滚动到对应区块并把该项标为当前项；不得卸载或隐藏其它区块 | 页面滚动到目标区块，导航项高亮 | BR-003 |
| R-04 | 分析已提交时 | 系统 | 顶部条应显示仓库标识、commit 短 SHA（取前 12 位）、当前阶段徽标 | 顶部条常驻显示当前分析上下文 | `types.ts:74-127` |
| R-05 | 尚未提交分析时 | 系统 | 顶部条应显示「未选择仓库」空态文案，不得显示空白区域 | 空态可读 | AE-08 |
| R-06 | 渲染任何界面元素时 | 系统 | 应只使用以 `#303d67` 为基调派生的深蓝色阶、`#79799a` 系次级文字/边框、`#fddfdc` 系强调色 | 全局配色统一 | D1（配色参考图） |
| R-07 | 渲染渐变时 | 系统 | 应只在品牌条、进度填充、统计卡顶部发丝线三处使用 `#303d67 → #79799a → #fddfdc` 渐变 | 渐变作为点睛而非底噪 | 设计约束，见 Evidence |
| R-08 | 渲染报告正文时 | 系统 | 应按文档式排版：阅读列宽上限 72ch、正文行高不低于 1.7、层级差异用字号与间距表达 | 长报告可读性提升 | D1（Notion 式排版） |
| R-09 | 渲染引用路径与行号时 | 系统 | 应用等宽字体完整呈现，不截断、不移入悬浮层；超宽时容器横向滚动而非撑破布局 | 引用可核验 | U13 验收项、`ReportView.tsx:1-9` |
| R-10 | 分析成功且结果就绪时 | 系统 | 应在结果区顶部渲染 4 张统计卡：评审发现总数、通过校验的引用总数、索引切块数、未完成模块数 | 报告质量一眼可见 | D1 预览 |
| R-11 | 渲染统计数据时 | 系统 | 应只由 `review.findings`、`review.outcomes`、`index.chunk_count`、`report.sections[].claims[].citations`、`report.missing`、`module_failures` 派生，不新增请求、不新增后端字段 | 无感知差异 | BR-002 |
| R-12 | 评审存在发现时 | 系统 | 应渲染严重度分布条，按高/中/低分段并显示各段计数 | 风险构成一眼可见 | D1 预览 |
| R-13 | 评审无发现或未执行时 | 系统 | 统计卡与分布条应显示 0 并保留「已执行零命中」与「未执行」的区分，不得渲染空白卡 | 零命中与未执行仍可区分 | R17、`ReviewFindings.tsx:1-7` |
| R-14 | 渲染统计区时 | 系统 | 不得呈现 Agent 执行过程信息（per-module 状态、阶段耗时、token 成本、工具调用序列） | 界面不含执行过程视图 | BR-001（plan R23） |
| R-15 | 渲染统计区时 | 系统 | 不得新增 `role="status"` 或 `role="alert"` 节点 | 读屏播报不被稀释 | BR-004 |
| R-16 | 提交分析后 / 分析完成后 | 系统 | 焦点应分别移至 `分析进度` 与 `架构报告` 标题，阶段变化仍由 aria-live 播报 | 键盘与读屏体验不退化 | U13 可访问交互要求 |
| R-17 | 用户使用键盘操作时 | 用户 | 导航项、输入框、按钮、折叠区应全部可 Tab 到达且焦点态可见（强调色焦点环） | 键盘可用 | U13 可访问交互要求 |
| R-18 | 渲染主题时 | 系统 | 应只提供深色主题，不提供浅色切换入口 | 单一主题 | D1（配色参考图为深色渐变） |

业务规则：

- BR-001：plan 的 R23「界面不承担 Agent 执行过程的可视化展示」不放宽。界面只呈现分析产物及其静态统计，不呈现执行过程。
- BR-002：后端零改动。本需求不得新增 API、不得新增响应字段、不得改变请求次数。
- BR-003：现有 40 个前端测试的语义契约不破坏。完成态下 `架构报告`/`代码评审`/`代码问答` 三个标题必须同时存在于 DOM，因此侧栏导航只能是锚点滚动式，不能是视图切换式。
- BR-004：`role="status"` 与 `role="alert"` 在任一时刻的匹配数量不得增加（测试用单数查询 `getByRole('status')` / `findByRole('alert')`）。
- BR-005：类名 `.progress__detail` 保留（`App.test.tsx:247,302` 用它做选择器）。
- BR-006：不引入任何前端 UI 库或图表库依赖，`package.json` 的 `dependencies` 保持 react + react-dom 两项。

优先级分级：

| 编号 | 优先级 | 可降级方案 | 是否阻塞上线 |
| --- | --- | --- | --- |
| R-01 | P0 / Must | 无（外壳是改版本体） | 是 |
| R-02 | P0 / Must | 无（U13 已承诺窄视口不溢出） | 是 |
| R-03 | P0 / Must | 降级为纯锚点跳转，不做高亮 | 是 |
| R-04 | P0 / Must | 降级为只显示仓库名，不显示 SHA | 是 |
| R-05 | P1 / Should | 降级为隐藏顶部条 | 否 |
| R-06 | P0 / Must | 无（配色是用户明确要求） | 是 |
| R-07 | P1 / Should | 降级为纯色，不用渐变 | 否 |
| R-08 | P0 / Must | 降级为固定列宽，不做 ch 单位上限 | 是 |
| R-09 | P0 / Must | 不可降级（可核验性底线） | 是 |
| R-10 | P0 / Must | 降级为 2 张卡（发现数、切块数） | 是 |
| R-11 | P0 / Must | 不可降级（BR-002 后端零改动） | 是 |
| R-12 | P1 / Should | 降级为纯文本计数，不做分布条 | 否 |
| R-13 | P0 / Must | 不可降级（R17 的零命中区分是已签需求） | 是 |
| R-14 | P0 / Must | 不可降级（BR-001，plan R23） | 是 |
| R-15 | P0 / Must | 不可降级（BR-004） | 是 |
| R-16 | P0 / Must | 不可降级（U13 可访问交互已签） | 是 |
| R-17 | P0 / Must | 不可降级（同上） | 是 |
| R-18 | P2 / Could | 后续可加浅色主题 | 否 |

## Acceptance Examples

```text
AE-01（对应 R-01、R-03）
Given 分析已完成，报告、评审、问答三个区块均已渲染
When 用户点击侧栏导航的「评审」
Then 页面滚动到「代码评审」区块且该导航项标为当前项
And 「架构报告」「代码评审」「代码问答」三个标题仍全部存在于 DOM

AE-02（对应 R-10、R-11、R-12）
Given 分析完成，review 含 3 条发现（高 1、中 1、低 1），index.chunk_count 为 12
When 结果区渲染
Then 统计卡显示发现数 3 与切块数 12
And 严重度分布条显示高/中/低三段及各段计数
And 期间未发起任何新的后端请求

AE-03（对应 R-13，异常）
Given 分析完成，review.findings 为空且 security 类别 status 为 skipped
When 结果区渲染
Then 统计卡显示发现数 0
And 界面仍能读出 structural 已执行且命中 0 条、security 未执行及其原因
And 不渲染空白统计卡

AE-04（对应 R-16）
Given 用户提交仓库地址
When 进入分析中状态
Then 焦点位于「分析进度」标题
And 收到 done 事件后焦点移至「架构报告」标题

AE-05（对应 R-15，异常）
Given 分析进行中且 SSE 连接断开
When 断连提示渲染
Then 页面上 role="alert" 的节点仍只有一个（断连提示本身）
And role="status" 的节点仍只有一个（进度状态）

AE-06（对应 R-09、R-02）
Given 视口宽度 375px，报告含长引用路径
When 报告渲染
Then 引用路径与行号完整可见且其容器可横向滚动
And 页面本身不产生横向溢出

AE-07（对应 R-14，异常）
Given 分析完成
When 统计区渲染
Then 统计区不出现模块名称、阶段耗时、token 数量或工具调用序列

AE-08（对应 R-05）
Given 用户尚未提交任何仓库
When 界面首次渲染
Then 顶部条显示「未选择仓库」空态文案而非空白区域

AE-09（对应 R-04）
Given 分析完成，仓库为 acme/widget 且 commit_sha 为 abcdef123456789
When 顶部条渲染
Then 顶部条显示仓库标识 acme/widget、commit 前 12 位 abcdef123456、当前阶段徽标

AE-10（对应 R-06、R-07）
Given 界面已渲染
When 检查任一元素的背景、文字、边框取值
Then 取值均来自以 #303d67 / #79799a / #fddfdc 派生的 CSS 自定义属性，无散落的硬编码色值
And 渐变只出现在品牌条、进度填充、统计卡顶部发丝线三处

AE-11（对应 R-08）
Given 报告含多节结论
When 报告正文渲染
Then 正文容器阅读列宽不超过 72ch，行高不低于 1.7
And 节标题与结论文本的层级差异由字号与间距表达，不依赖分隔线

AE-12（对应 R-17）
Given 分析完成，界面含导航项、输入框、按钮与折叠区
When 用户仅用 Tab 与 Enter 操作
Then 上述元素全部可达且当前焦点元素显示强调色焦点环
And 可完成「跳到评审区、展开检查范围、提交一个问题」整条链路

AE-13（对应 R-18，异常）
Given 界面已渲染
When 用户查找主题切换入口
Then 界面不提供浅色主题切换入口，且在系统偏好为浅色时仍呈现深色工作台
```

## Negative Acceptance

```text
NA-01
Given 本需求实施
When 改动落地
Then 不得新增或修改任何后端文件、API 路径或响应字段

NA-02
Given 侧栏导航实现
When 用户切换导航项
Then 不得卸载或以 display:none 隐藏任一结果区块（否则完成态下三个标题无法同时存在）

NA-03
Given 统计区实现
When 统计区渲染
Then 不得新增 role="status" / role="alert" / aria-live 容器

NA-04
Given 本需求实施
When 改动落地
Then 不得修改 plan 的 R23 或 Scope Boundaries 条款

NA-05
Given 本需求实施
When 依赖清单变更
Then package.json 的 dependencies 不得新增条目
```

## Scope Boundaries

### 本期做

- 视觉层全量替换：配色令牌、字体尺度、间距节奏、圆角、边框、投影。
- 布局外壳：左侧锚点导航栏、顶部仓库标识条、独立滚动的主内容区。
- 结果侧统计条：4 张统计卡 + 严重度分布条，全部由现有字段派生。
- 报告正文的文档式排版（阅读列宽、行高、层级节奏）。
- 现有 40 个前端测试的适配（仅在语义未变的前提下调整查询，不删除断言）。

### 本期不做（Non-Goals）

- Agent 执行过程可视化：per-module 实时状态矩阵、阶段耗时、token 成本、工具调用 trace。plan 的 R23 不放宽（D1 决定）。
- 后端任何改动：不加字段、不加接口、不改请求次数。
- 时间序列图表、历史分析对比、多仓库看板。现有 API 无时间序列字段。
- 浏览式 wiki 形态的代码浏览器或文件树。plan 的 Scope Boundaries 明列在产品定位之外。
- 路由与多页面。plan 的 Implementation Scope Boundaries 明确单页无路由。
- 浅色主题与主题切换入口。
- 多用户、账号、协作、分享。

### 与其它模块/需求的关系

- 依赖 `docs/plans/2026-08-23-001-feat-codepilot-agent-plan.md` 的 U13「React 前端」章节：状态矩阵、可访问交互、响应式三段要求本次全部继承，不重新定义。
- 依赖同文件 Product Contract 的 R22、R23：R22 的五件事是界面能力上限，R23 是本次统计区设计的硬边界。
- 不依赖 U12（FastAPI 后端与 SSE）的任何变更。

## Evidence And Assumptions

| 主张 | 类型 | 证据来源 / 为何是假设 | 确认路径 |
| --- | --- | --- | --- |
| 现有界面为单列纵向堆叠、无侧栏无路由 | confirmed-source | `frontend/src/App.tsx:141-203` 已读 | source |
| 完成态下三个区块标题同时挂载 | confirmed-source | `frontend/src/App.tsx:177-201` 已读；`App.test.tsx` 三个 describe 各自 await 自己的标题 | source |
| 40 个前端测试 / 693 个后端测试当前全绿 | confirmed-source | 本次实跑 `npm test`、`pytest -q` | source |
| 现有 API 无耗时/成本/时间序列字段 | confirmed-source | `frontend/src/api/types.ts` 全文已读 | source |
| 统计条所需字段均已存在于 `AnalysisResult` | confirmed-source | `types.ts:85-127` 已读 | source |
| 基调色为 `#303d67`、过渡色 `#79799a`、辅助色 `#fddfdc` | user-stated | 用户提供配色参考图，图中标注三个色值 | 当前执行对话用户 |
| 工作台三区布局（侧栏 + 顶部条 + 内容区）符合用户预期 | user-stated | D1 选项含 ASCII 预览，用户选定该预览 | 当前执行对话用户 |
| 只做深色主题 | assumption | 配色参考图为深蓝渐变，用户表述为「主基调背景色」，推断为深色底 | 已记入 R-18，可由用户后续推翻 |
| 4 张统计卡的具体口径（发现数/引用覆盖/切块数/未完成模块数） | user-stated | D1 预览中列出「发现 12 / 覆盖 94 / 块 318 / 跳 2」四项 | 当前执行对话用户 |
| 渐变限用三处 | assumption | 参考图为大面积渐变，但界面大面积渐变会压过正文可读性；限制为设计判断 | 已记入 R-07，可降级 |
| API key 泄露风险由用户降级 | user-stated | D2：`.env` 指向第三方聚合站的测试 key，用户明确不用管 | 当前执行对话用户 |

本需求不触及权限、用户数据、资金/交易、审计或合规。界面无鉴权层（plan 已把私有仓库接入与鉴权列为 Deferred）。

## Interaction Requirements

| 元素 | 是否必须 | 展示/交互规则 | 文案 | 风险 / 约束 |
| --- | --- | --- | --- | --- |
| 侧栏导航项 | 是 | 锚点滚动 + 当前项高亮；结果未就绪时对应项置灰不可点 | 概览 / 报告 / 评审 / 问答 | 必须锚点式，不得视图切换（BR-003） |
| 顶部仓库标识条 | 是 | 常驻；已提交时显示仓库名 + commit 前 12 位 + 阶段徽标 | 未提交时「未选择仓库」 | 不得与报告内的仓库元信息冲突显示矛盾值 |
| 提交输入区 | 是 | 保持现有 form 元素 + label + 回车提交 + 禁用原因提示 | 现有文案不改 | 现有测试按 label 与按钮名查询，文案不可改 |
| 统计卡 | 是 | 4 张等宽卡；数值用等宽数字（tabular-nums）；零值显示 0 | 发现 / 引用 / 切块 / 未完成 | 不得挂 role/aria-live（R-15） |
| 严重度分布条 | 否 | 按高/中/低分段的横向比例条，各段带计数标签 | 高危 N / 中危 N / 低危 N | 标签文本必须与 `ReviewFindings` 的「高」「中」「低」徽标文本不同，避免 getByText 多匹配 |
| 报告正文 | 是 | 阅读列宽 ≤72ch，行高 ≥1.7，结论与引用视觉分层 | 现有文案不改 | 引用不得截断（R-09） |
| 折叠区 | 是 | 继续用原生 details 与 summary 元素 | 现有文案不改 | 现有测试按 summary 文本查询 |
| 焦点态 | 是 | 所有可交互元素显示强调色焦点环 | — | 焦点顺序与现有契约一致（R-16） |

## Exception Handling

| 场景 | 系统表现 | 用户提示 | 是否可重试 | 是否产生状态/数据/审计副作用 |
| --- | --- | --- | --- | --- |
| 结果未就绪时点击侧栏「报告」 | 该导航项置灰不响应 | 不提示（置灰即答案） | 是 | 否 |
| review 为 null | 统计卡发现数显示 0，分布条不渲染 | 保留评审区自身的空态说明 | 否 | 否 |
| index 为 null | 切块数卡显示「—」而非 0 | 无索引与零切块含义不同 | 否 | 否 |
| report 为 null 但分析成功 | 统计卡引用覆盖显示「—」，报告区显示现有空态文案 | 沿用现有「报告没有通过校验的结论」 | 否 | 否 |
| SSE 断连 | 沿用现有断连提示与重连按钮，统计区不渲染 | 现有文案不改 | 是 | 否 |
| 窄视口长引用路径 | 引用容器横向滚动 | 无 | — | 否 |

## Design Source Coverage

design_source_inventory:
- source_or_node: 用户提供的配色参考图（渐变配色卡「深蓝柔粉」，标注 `#303d67` / `#79799a` / `#fddfdc`）
  read_status: read
  affected_prd_write_targets: Interaction Requirements | Requirements | Evidence And Assumptions
  extracted_design_what: 三个色值及其角色定位（深蓝为基调、灰紫为过渡、柔粉为辅助）；整体气质为柔和克制的深色渐变
  evidence_level: confirmed owner/source
  unread_or_degraded_reason: 无
  readiness_consequence: 配色令牌可直接派生，无残留
  conflicts:
    - contradicts: 无
      owner_authority_needed: no
      readiness_consequence: 无
- source_or_node: D1 选项内的 ASCII 布局预览（侧栏 + 顶部条 + 统计卡 + 分布条 + 正文）
  read_status: read
  affected_prd_write_targets: Requirements | Acceptance Examples | Interaction Requirements
  extracted_design_what: 三区网格结构、导航四项、统计卡四张、严重度分布条位置、正文排版风格
  evidence_level: confirmed owner/source
  unread_or_degraded_reason: 无
  readiness_consequence: 布局结构由 owner 选定该预览而确认，无残留
  conflicts:
    - contradicts: 无
      owner_authority_needed: no
      readiness_consequence: 无

design_sources_read:
- 配色参考图 + 色值 → Requirements R-06/R-07、Evidence And Assumptions，evidence_level: confirmed owner/source
- D1 ASCII 布局预览 → Requirements R-01/R-03/R-10/R-12、Acceptance Examples，evidence_level: confirmed owner/source

design_sources_unread:
- none

design_source_coverage: read
design_degraded_owner_acceptance_ref: none

说明：本需求没有 Figma 文件、组件规范或交互态设计稿。间距尺度、圆角半径、字号阶梯、投影强度属于视觉实现细节，由 `spec-plan`/实现阶段在 R-06 与 R-08 的约束内决定，不构成 WHAT 缺口。

## Decision Notes

- **D1 保 R23，Grafana 味道用结果侧统计实现。** 用户原始表述含 Grafana，而 Grafana 的语义是可观测面板，与 plan 的 R23「界面不承担 Agent 执行过程的可视化展示」及 Scope Boundaries 的「Agent 执行过程的可视化展示属产品定位之外」正面冲突；且真仪表盘需要耗时/成本/时间序列字段，现有 API 全无。用户选定「保 R23 + 结果侧统计面板」，因此统计只覆盖分析产物的静态构成，plan 不改，后端不改。
- **D2 API key 泄露风险由用户降级。** backlog 首条警示要求轮换两个明文外泄的 key。用户说明 `.env` 指向第三方聚合站的测试 key，不需处理。本 PRD 记录该决定；backlog 中该警示相应下调，但不删除（记录仍有价值）。
- **D3 授权一次完整分析，U9 全量索引不做。** 用于补 U8 报告端到端证据与 U7 两处修正的实跑覆盖。
- **D4 U13-U15 按用户口述记账。** docs/ 下无对应证据文件，backlog 也仍列在「需你操作」；用户选择接受口述。清单状态与证据强度因此不一致，该不一致在 backlog 与本 PRD 中显式标注。
- **锚点导航而非视图切换**（源自 BR-003）。现有 40 个测试中，报告/评审/问答三个 describe 各自 await 自己的 `h2`，而三者出现在同一个完成态下。若侧栏做成视图切换，任一时刻只有一个区块在 DOM，三组测试会同时失败。锚点滚动同时满足 D1 的工作台观感与 plan 的「单页应用，无路由」。
- **统计区不挂 role。** 现有测试用 `getByRole('status')` 与 `findByRole('alert')` 单数查询，新增同名 role 会导致多匹配报错。统计是静态派生数据，本身也不需要播报。

## Planning Recheck

| item | why recheck | required before | blocks planning? |
| --- | --- | --- | --- |
| 「引用覆盖数」的口径 | 本 PRD 定为「通过校验的引用总数」，即 `report.sections[].claims[].citations` 的计数；与 `validation_summary` 文案口径是否一致需实现时核对一次 | 统计卡实现 | no |
| 严重度取值集合 | `SEVERITY_LABELS` 现只映射 high/medium/low；后端若返回其它值，分布条需有兜底段 | 分布条实现 | no |

## Outstanding Questions

| id | question | prd write target | blocks_planning | closure_disposition | planning_would_invent_what | closure_state | recommended default |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OQ-1 | 工作台是否放宽 plan 的 R23 以承载 Agent 执行过程面板 | Requirements R-14、Scope Boundaries、BR-001 | no | owner-answered | no | closed | 保 R23，统计只覆盖分析产物（用户已选定） |
| OQ-2 | 统计条的四项口径取哪些字段 | Requirements R-10、R-11 | no | owner-answered | no | closed | 发现数 / 引用覆盖 / 切块数 / 未完成模块数（D1 预览已列出） |
| OQ-3 | 侧栏导航是锚点滚动还是视图切换 | Requirements R-03、BR-003、Decision Notes | no | source-resolved | no | closed | 锚点滚动。证据：`frontend/src/test/App.test.tsx:475-660` 三组 describe 在同一完成态下各自 await 自己的 h2，视图切换会使三者无法共存 |
| OQ-4 | 是否提供浅色主题 | Requirements R-18 | no | owner-accepted-assumption | no | closed | 只做深色。用户提供的配色图为深蓝渐变并表述为「主基调背景色」 |
| OQ-5 | 间距/圆角/字号阶梯的具体数值 | Interaction Requirements | no | implementation-only-how-pushdown | no | closed | 由实现阶段在 R-06/R-08 约束内决定；不触及接口可用性、权限、范围、source-of-truth、兜底展示或统计验收 |

## Owner Decision Trace

| question | owner_answer | chosen_answer | prd write target | consequence | closure_state |
| --- | --- | --- | --- | --- | --- |
| OQ-1：UI 改版要不要突破 R23「界面不承担 Agent 执行过程可视化」这条已签约束 | 选定「保 R23 + 结果侧统计面板」，并选定该选项内的 ASCII 布局预览 | 保 R23：视觉与布局壳全重做，Grafana 味道用分析结果统计实现，只用现有字段，后端零改动，plan 不改 | Requirements R-01/R-03/R-10/R-11/R-14、Scope Boundaries、BR-001/BR-002、Decision Notes | 统计区不得含执行过程信息；后端不动；plan 条款不改 | closed |
| OQ-2：统计条的四项口径取哪些字段 | 同上，选定预览中「发现 12 / 覆盖 94 / 块 318 / 跳 2」四项 | 评审发现总数、通过校验的引用总数、索引切块数、未完成模块数 | Requirements R-10、R-11、Acceptance Examples AE-02 | 四项全部可由现有 `AnalysisResult` 派生，无需后端加字段 | closed |
| OQ-4：是否提供浅色主题 | 未单独提问；用户表述为「配色按照这个为主基调背景色」，参考图为深蓝渐变 | 只做深色主题 | Requirements R-18 | 不做主题切换入口；后续可推翻 | closed |
| backlog 首条警示：两个 API key 明文进过对话记录，是否已轮换 | 「聚合站测试 key，不用管」 | 不轮换，风险由用户接受 | Decision Notes D2、Evidence And Assumptions | backlog 该警示下调但不删除 | closed |
| 剩余清单的真实调用成本授权 | 「一次完整分析，U9 仍抽样」 | 授权一次基准仓库完整分析；U9 全量索引不做 | Decision Notes D3 | U8 报告端到端与 U7 修正实跑可补；U9 检索质量结论仍受抽样限制 | closed |
| U13-U15 的验证证据怎么处理 | 「接受口述，只更新清单状态」 | backlog 中 U13-U15 移入已验证，证据列标注为用户口述、无输出文件 | Decision Notes D4 | 清单状态与证据强度不一致，需显式标注 | closed |

## 需求追溯矩阵

| 需求编号 | 关联业务规则 | 验收编号 | 约束 / 风险 | 证据 / 规则依据 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| R-01 | — | AE-01 | 外壳重构可能影响现有 DOM 层级 | D1 预览 | P0 |
| R-02 | — | AE-06 | 窄视口横向溢出 | U13 响应式要求 | P0 |
| R-03 | BR-003 | AE-01 | 视图切换会破坏三标题共存 | `App.test.tsx:475-660` | P0 |
| R-04 | — | AE-08 | — | `types.ts:74-127` | P0 |
| R-05 | — | AE-08 | — | 空态设计判断 | P1 |
| R-06 | — | — | — | 配色参考图 | P0 |
| R-07 | — | — | 渐变过量压过正文可读性 | 设计判断，可降级 | P1 |
| R-08 | — | — | — | D1（Notion 式排版） | P0 |
| R-09 | — | AE-06 | 截断会破坏可核验性 | `ReportView.tsx:1-9` | P0 |
| R-10 | BR-002 | AE-02、AE-03 | — | D1 预览 | P0 |
| R-11 | BR-002 | AE-02 | 新增请求会违反后端零改动 | `types.ts:85-127` | P0 |
| R-12 | — | AE-02 | 标签文本与评审徽标冲突会导致测试多匹配 | D1 预览 | P1 |
| R-13 | — | AE-03 | 零命中与未执行混同 | R17、`ReviewFindings.tsx:1-7` | P0 |
| R-14 | BR-001 | AE-07 | 违反 plan R23 | plan R23 | P0 |
| R-15 | BR-004 | AE-05 | role 多匹配导致测试失败 | `App.test.tsx` 单数查询 | P0 |
| R-16 | — | AE-04 | 焦点契约退化 | U13 可访问交互 | P0 |
| R-17 | — | — | 键盘不可达 | U13 可访问交互 | P0 |
| R-18 | — | — | — | 配色参考图（assumption） | P2 |

## Readiness Self-Check

write_mode: route-out
clarification_evidence: asked-owner
preflight_sweep_closure: closed
decision_card_highest_risk_gap: 工作台形态（Grafana/GitHub 语义）与 plan R23 及 Scope Boundaries 正面冲突，且真仪表盘需要现有 API 不存在的耗时/成本/时间序列字段
decision_card_next_action: route-out
decision_card_why_no_invention: R23 边界、统计四项口径、导航形态、配色三色值、后端零改动全部已由 owner 决定或 source 证据闭合；布局结构由 owner 选定的 ASCII 预览锁定；剩余仅间距/圆角/字号等 HOW 细节，planning 不需发明任何产品行为
design_source_coverage: read
first_unclosed_owner_question: none
recommended default: none
can_enter_spec_plan: no
why_not: 已被 2026-08-25-003-code-intelligence-workbench 取代；BR-001/002/003/006 四条硬约束经 owner 推翻，planning 只认 003

## 变更记录

| 日期 | 修改人 | 变更内容 |
| --- | --- | --- |
| 2026-08-25 | zzhelp（经 spec-prd） | 初稿。含 D1-D4 owner 决定、R23 冲突裁定与结果侧统计口径。 |

## Handoff

- 本 PRD 的 WHAT 已闭合，可进入 `spec-plan` 决定实现顺序与文件级改动。
- 实施时必须带上三条硬约束：BR-003（三标题共存 → 锚点导航）、BR-004（不新增 role）、BR-005（保留 `.progress__detail`）。
- 不改 plan 的 R22/R23 与 Scope Boundaries；本 PRD 是它们之下的界面层增量。







