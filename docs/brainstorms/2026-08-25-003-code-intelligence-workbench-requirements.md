---
spec_id: 2026-08-25-003-code-intelligence-workbench
artifact_kind: prd-requirements
target_surface: Mixed
status: ready-for-planning
evidence_grade: mixed
source_authority: mixed
readiness_authority: engineering-owned
created: 2026-08-25
source_inputs:
  - docs/brainstorms/2026-08-25-002-ui-workbench-redesign-requirements.md
  - docs/plans/2026-08-23-001-feat-codepilot-agent-plan.md
  - README.md
  - backend/api/routes.py
  - backend/api/schemas.py
  - backend/config.py
  - backend/review/models.py
  - backend/static_analysis/models.py
  - backend/mcp_server/server.py
  - frontend/src/App.tsx
  - frontend/src/styles.css
  - frontend/src/test/App.test.tsx
  - frontend/src/test/Workbench.test.tsx
write_mode: final-prd
can_enter_spec_plan: yes
clarification_evidence: asked-owner
preflight_sweep_closure: closed
next_owner_question: none
readiness_verified_by: check-prd-artifact.js
readiness_verified_at: 2026-08-26T14:51:18.829Z
readiness_checker_schema: spec-prd-artifact-check.v1
readiness_finding_count: 1
readiness_blocking_count: 0
readiness_prd_hash: sha256:817a36253c7ada601f57ee8f31c448a3cd6cc72e9f0cfdd23b90c4b6ad35d96b
readiness_inputs_hash: sha256:a0d8bf1a7a4d27374d95e27b937e68098c779495e8cc7788403185b050019347
---

# CodePilot Code Intelligence 工作台 增量需求文档

## PRD 元数据

| 项 | 内容 |
| --- | --- |
| 需求名称 | Code Intelligence 工作台（多页形态 + 公网部署） |
| 需求编号 / spec_id | 2026-08-25-003-code-intelligence-workbench |
| 业务域 | 前端页面层 + 后端 API 层 + 部署形态 |
| 目标 surface | Mixed（H5/PC 前端新增 9 个页面 + Backend 新增 5 类端点） |
| 目标地区 / 市场 / tenant | 不涉及 |
| 目标用户 / 客户类型 | 作品评审者（公网访客，自带 LLM key）与项目作者本人 |
| 是否触及付费、资金或交易 | 否（LLM 额度由访客自己的 key 承担） |
| 是否触及个人信息或敏感数据 | 访客的 LLM API key 经浏览器存储与请求透传，服务端不落盘 |
| 是否需要外部规则或专业意见 | 否 |
| 相关文档 | `docs/brainstorms/2026-08-25-002-ui-workbench-redesign-requirements.md`（被本文件 supersede）；`docs/plans/2026-08-23-001-feat-codepilot-agent-plan.md`（R22/R23、U12/U13、Scope Boundaries） |

## Summary

把 CodePilot 从「单页锚点滚动的分析结果展示器」改为「多页的代码情报工作台」：9 个左侧导航页（Overview / Architecture / AI Chat / Code Search / Security Review / Error Handling / Structural / Reports / MCP）、依赖图可视化、VS Code 三栏代码查看器、报告导出，并首次具备公网部署形态——访客自带 LLM key，服务端不存凭证，靠 IP 限流与并发闸门护住服务器资源。

本次推翻既有 PRD 的四条硬约束：后端不再零改动（新增 5 类端点）、引入图形渲染能力、废除「三标题共存」的锚点导航、去掉 Performance 页（后端无此类检查）。所有展示数字只用后端真实字段派生，不引入评分、星级或技术债估算。

## Problem Frame

现有界面是单页纵向流，五个能力区块靠锚点滚动定位。它能展示分析结果，但三件事做不到：

第一，**分析产物只能读不能查**。报告给出「认证逻辑集中在 auth/jwt.strategy.ts」并附行号，但读者点不进那个文件——引用可核验的设计在界面上止步于「显示路径」。第二，**依赖图与模块聚类是后端已算出的结构化数据，界面只呈现为文字**。图问题用文字描述，读者要在脑子里重建拓扑。第三，**只能本地跑**。作品的第一接触面是一个需要评审者 clone 仓库、配 key、起容器才能看到的东西。

不做的代价：后端 721 个测试、可追溯报告、三类评审检查、MCP server 这些实质工作，被「必须本地部署才能看」和「结论无法点进去核验」两道门槛挡在评审者之外。

## Change Delta

| 变化类型 | 内容 | 涉及现有能力 | 用户/数据/运营影响 | 证据 tag |
| --- | --- | --- | --- | --- |
| keep | 分析流水线全部不动：ingest / parse / cluster / planner / module_agent / synthesize / reviewer / chunk_and_index 八个节点与 SSE 生命周期 | `backend/graph/builder.py` | 无行为变化 | confirmed-source: `backend/graph/builder.py:149-182` |
| keep | 引用校验链路不动：报告结论必须过 `validate_report` 才进产出 | `backend/report/validate.py` | 无 | confirmed-source: `backend/report/validate.py:209-223` |
| keep | 路径逃逸防护不动，新增的文件读取端点必须复用 `resolve_within` | `backend/paths.py` | 新端点不放宽安全边界 | confirmed-source: `backend/paths.py`（KTD14/KTD15） |
| keep | 配色令牌沿用 002 已落地的 `#303d67` / `#79799a` / `#fddfdc` 色阶 | `frontend/src/styles.css:12-37` | 视觉延续 | confirmed-source: `frontend/src/styles.css:4-37` |
| replace | 导航形态：锚点滚动（三区块共存）→ 客户端路由多页切换（任一时刻一页在 DOM） | `App.tsx` 根结构、`App.test.tsx` 三组 describe | 9 个导航页可实现；15~20 个测试需改写 | user-stated（D3）+ confirmed-source: `App.test.tsx:475-660` |
| replace | 顶部概览口径：方案原始的 Stars / 代码行数 / 技术债 / Agent 评分 → 文件数、模块数、发现数、切块数、语言分布 | 新增顶部条 | 每个数字可指回字段来源 | user-stated（D2） |
| replace | 报告页评分条 → 评审执行情况表 + 引用校验通过率 + 缺失说明 | 新增报告页 | 不出现编造的分数 | user-stated（D2） |
| extend | 新增 Architecture 页：模块节点图 + 点击节点看详情 | 复用 `modules`、`dependency_graph`、`Symbol` | 依赖拓扑可视 | user-stated（D4） |
| extend | 新增代码查看器：文件树 + 代码 + 该文件的评审发现三栏 | 需新增文件内容端点 | 结论可点进去核验 | user-stated（D4、D9） |
| extend | 新增 Code Search 页：向量语义检索 | 复用 MCP 的 `search_code` 逻辑，需新增 HTTP 端点 | 语义找代码 | confirmed-source: `backend/mcp_server/server.py:262-302` |
| extend | 新增报告导出：Markdown / HTML / PDF | 由现有 `report` 字段渲染；PDF 需新依赖 | 报告可带走 | user-stated（D4） |
| extend | 新增最近分析持久化：`AnalysisResult` 落盘为 JSON，启动时扫目录 | 现在任务状态在进程内存，重启即丢 | 历史可读 | user-stated（D7）+ confirmed-source: `README.md:141-143` |
| extend | 新增设置页：访客填自己的 LLM base_url / key / 模型名，存浏览器 | 现在只能改 `.env` | 访客自带凭证 | user-stated（D1） |
| extend | 新增 IP 限流 + 并发闸门 + 磁盘配额 | 现在无任何请求侧限制 | 公网下服务器资源有保护 | user-stated（D6） |
| remove | Performance 导航页（方案原有）：后端无此类检查，且与 plan R23 冲突 | — | 导航项从 9 项调整为 9 项但组成不同 | confirmed-source: `backend/review/models.py:20-26` |
| remove | 调用链子项（方案原有）：解析层不提取调用点 | — | Architecture 页只有模块图 | confirmed-source: `README.md:130-132` |
| remove | MCP 状态灯（方案原有）：stdio 子进程由客户端拉起，后端观测不到 | — | MCP 页只做配置说明 | confirmed-source: `README.md:95-96` |
| unknown | 无 | — | — | — |

历史逻辑说明：本文件 supersede `2026-08-25-002-ui-workbench-redesign`。该 PRD 的 BR-001（保 R23）、BR-002（后端零改动）、BR-003（三标题共存）、BR-006（不引入图表库）四条全部被 owner 在本次推翻。002 的视觉令牌成果（`styles.css` 的色阶体系）继续使用。

## Current System Snapshot

| 现状项 | 当前行为 | 证据 tag |
| --- | --- | --- |
| 前端形态 | 单页无路由，`App.tsx` 按 `Phase` 状态机渲染五个区块，完成态下报告/评审/问答三个 `h2` 同时挂载 | confirmed-source: `frontend/src/App.tsx:141-203` |
| 前端测试基线 | 53 个测试（`App.test.tsx` 39 + `Workbench.test.tsx` 14），8 + 2 个 describe 块 | confirmed-source: 本次实测 `grep -c "it("` |
| 前端依赖 | 仅 react + react-dom，无 UI 库、无图表库、无路由库 | confirmed-source: `frontend/package.json:14-17`（经 002 记录） |
| 现有 API 端点 | 6 个：POST `/api/analyses`、GET `/api/analyses`、GET `/api/analyses/{id}/events`、GET `/api/analyses/{id}`、POST `/api/analyses/{id}/questions`、GET `/api/health` | confirmed-source: `backend/api/routes.py:72-223` |
| 无文件读取端点 | HTTP 层无任何按路径返回文件内容的端点（`backend/tools/read_file.py` 只供 Agent 与 MCP 使用） | confirmed-source: 本次实测 `grep -rn "read_file\|/files" backend/api/` 无命中 |
| 无代码搜索端点 | HTTP 层无搜索端点；`search_code` 逻辑只在 MCP server 内 | confirmed-source: `backend/mcp_server/server.py:262-302` |
| 无鉴权层 | `backend/api/` 下无 auth / login / session 任何实现；README 明写「没有鉴权层」且两端口只绑 `127.0.0.1` | confirmed-source: `README.md:48-49,140` |
| 任务状态易失 | 分析任务状态在 `TaskRegistry` 的进程内存里，服务重启后未完成任务丢失；后端固定单 worker | confirmed-source: `README.md:141-143`、`backend/api/tasks.py:86-100` |
| 索引缓存已落盘 | 向量索引存具名卷 `codepilot-data`，二次分析命中缓存 | confirmed-source: `README.md:51-52` |
| 评审类别 | 只有 `structural` / `error_handling` / `security` 三类，无 performance、无通用 bug 类 | confirmed-source: `backend/review/models.py:20-26` |
| 评审发现字段 | `category`、`kind`、`path`、`line`、`message`、`evidence`、`severity`、`related_paths`；`line=0` 表示问题属文件/模块整体 | confirmed-source: `backend/review/models.py`（Finding 类） |
| 符号带行号 | `Symbol` 有 `name`、`kind`、`path`、`start_line`、`end_line`、`parent`，1-based 闭区间 | confirmed-source: `backend/static_analysis/models.py:21-35` |
| 模块字段 | `Module` 有 `name`、`files`、`internal_edges`、`external_edges`、`origin` | confirmed-source: `backend/static_analysis/models.py:145-154` |
| 模块分析产出 | `ModuleAnalysis.summary` 是自由文本，无结构化的「职责」「核心函数」字段 | confirmed-source: `backend/graph/state.py:36-47` |
| 无调用链 | 解析层不提取调用点，`find_references` 是依赖图定界的文本匹配 | confirmed-source: `README.md:130-132` |
| 无评分机制 | 后端无任何 score / rating / grade 产出；无代码行数、无 Stars、无技术债估算 | confirmed-source: 本次实测 `grep -rn "score\|rating\|grade" backend/report/ backend/review/` 仅命中 `rule_score`（Planner 内部排序用） |
| 无导出能力 | 无 PDF / MD / HTML 导出实现，无相关依赖 | confirmed-source: 本次实测 `grep -rn "pdf\|export\|download" backend/` 无相关命中 |
| MCP 传输方式 | stdio，由客户端自己拉起子进程，不在容器内 | confirmed-source: `README.md:95-96` |
| MCP 已有 7 个工具 | `list_repos`、`read_repo_structure`、`read_file`、`find_definition`、`find_references`、`query_dependencies`、`search_code` | confirmed-source: `backend/mcp_server/server.py:82-262` |
| 准入门限 | 本 PRD 撰写时为 `max_parseable_files=1500`、`max_repo_size_kb=300000`，超限拒绝。**实现期经 owner 要求放宽到 8000 / 1500000（见 Owner Decision Trace 的 OQ-14 行）**；此处保留撰写时的值，因为 Change Delta 与 Problem Frame 的推理以它为基线 | confirmed-source: `backend/config.py`（Settings） |
| 配色已落地 | `styles.css` 已有 `--anchor: #303d67`、`--ink-dim: #79799a`、`--accent: #fddfdc`、`--grad` 三色渐变令牌 | confirmed-source: `frontend/src/styles.css:12-37` |
| CORS 已限源 | `allow_origins` 不用通配符，因为该 API 能触发克隆与 LLM 调用（有成本） | confirmed-source: `backend/config.py`（注释） |

## Mixed 属性确认

| 维度 | 内容 |
| --- | --- |
| 涉及 surface | H5/PC（前端 9 页）+ Backend（新增 5 类端点与限流层） |
| 主业务结果 | 访客在公网用自己的 LLM key 提交仓库、多页浏览分析产物、点进任意文件核验结论、导出报告 |
| 当前 source-of-truth | LLM 凭证：服务端 `.env`；分析结果：进程内存；索引：具名卷 |
| 目标 source-of-truth | LLM 凭证：访客浏览器（服务端不存）；分析结果：`.workspace/analyses/*.json`（服务端落盘）；索引：具名卷（不变） |
| producer | 后端分析流水线（八节点图） |
| consumers | 前端 9 个页面、MCP 客户端（Cursor / Claude Code）、报告导出产物 |
| 同步方式 / 时效（产品语义） | 分析进度经 SSE 实时推送；结果落盘后对后续请求即时可读；导出取当次结果快照 |

## Source-Of-Truth Resolution

| item | current source | target source | non-authoritative mirrors | conflict rule | evidence |
| --- | --- | --- | --- | --- | --- |
| LLM 凭证（base_url / key / 模型名） | 服务端 `.env` | 访客浏览器本地存储 | 无（服务端不落盘、不记日志） | 访客未配置时不得回落到服务端 `.env`，必须提示配置 | user-stated（D1）+ `backend/config.py`（现状） |
| 嵌入模型凭证 | 服务端 `.env` | 服务端 `.env`（不变） | 无 | 索引由服务端统一构建，访客不可改，避免索引键分裂 | confirmed-source: `backend/cache/key.py`（provider_identity 参与缓存键） |
| 分析结果 | 进程内存 `TaskRegistry` | `.workspace/analyses/{task_id}.json` | 进程内存（在跑任务的实时态） | 内存与落盘冲突时以落盘为准；在跑任务只存在于内存 | user-stated（D7） |
| 向量索引 | 具名卷 `codepilot-data` | 不变 | 无 | 索引键不匹配时报「未建索引」而非「未找到」 | confirmed-source: `backend/mcp_server/server.py:285-292` |
| 仓库工作副本 | `.workspace/repos/{slug}` | 不变（新增总量配额） | 无 | 超配额时按 LRU 清理，不删正在分析的仓库 | user-stated（D6） |

## Producer / Consumer Map

| producer | artifact / state / config | consumers | expected freshness | change effect | failure visibility |
| --- | --- | --- | --- | --- | --- |
| 分析流水线 | `AnalysisResult`（报告 + 评审 + 索引状态） | Overview / Architecture / Reports / 三个评审页 / 代码查看器右栏 | 分析完成即刻 | 结果落盘后各页共读同一份 | 页面显示「该分析不存在或已被清理」 |
| 分析流水线 | `.workspace/analyses/{task_id}.json` | 首页「最近分析」列表 | 落盘即刻 | 重启后列表仍可读 | 文件损坏时该条目跳过并计数 |
| 静态解析层 | `modules` + `dependency_graph` + `Symbol` 表 | Architecture 页节点图与节点详情 | 随分析结果 | 图降级为目录级时节点粒度变粗 | 节点图显示降级说明 |
| 评审链路 | `Finding[]`（含 path + line） | 三个评审页、代码查看器右栏 | 随分析结果 | 零命中与未执行必须可区分 | 未执行时显示原因而非空列表 |
| 向量索引 | 索引块 | Code Search 页、AI Chat 页 | 分析完成即刻 | 索引键变更导致检索不可用 | 显示「未建索引」而非「未找到」 |
| 访客浏览器 | LLM 凭证 | 后端 LLM 调用（请求内透传） | 每次请求 | 凭证无效时分析立即失败 | 提示「你配置的 LLM 凭证无效」并指向设置页 |
| 后端 MCP server | 7 个工具 | Cursor / Claude Code | 客户端自行拉起 | 后端无法观测其运行状态 | MCP 页只给配置说明，不显示状态 |

## Requirements

### 导航与页面结构

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-01 | 打开界面时 | 系统 | 应以「左侧导航栏 + 顶部项目概览条 + 主内容区」渲染工作台，左侧导航固定为 9 项：Overview、Architecture、AI Chat、Code Search、Security Review、Error Handling、Structural、Reports、MCP | 9 项导航常驻可见 | D5 |
| R-02 | 用户点击导航项时 | 用户 | 系统应切换主内容区为该页，卸载其它页；URL 应随之变化且可直接访问与刷新 | 页面切换，URL 反映当前页 | D3 |
| R-03 | 分析结果尚未就绪时 | 系统 | 除 Overview、Settings、MCP 外的导航项应置为不可点并标注原因 | 未就绪的页不可进入且原因可读 | AE-11 |
| R-04 | 视口宽度小于 960px 时 | 系统 | 应将左侧导航转为顶部横向或抽屉式导航，主内容区占满宽度且不横向溢出 | 窄视口可用 | plan U13 响应式要求 |
| R-05 | 用户直接访问某页 URL 而该分析不存在时 | 用户 | 系统应显示「该分析不存在或已被清理」并给出返回首页入口，不得显示空白页 | 失效链接可解释 | D7 的落盘清理后果 |

### 顶部概览条（口径硬约束）

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-06 | 分析结果就绪时 | 系统 | 顶部概览条应显示：仓库标识、commit 短 SHA、当前阶段、文件总数、模块数、评审发现总数、索引切块数、语言分布 | 项目规模与产出规模一眼可见 | D2 |
| R-07 | 渲染顶部概览条时 | 系统 | 不得显示 Stars、代码行数、技术债估时、Agent 星级评分或任何评分数值 | 界面不含无数据源的数字 | BR-002 |
| R-08 | 渲染任何统计数字时 | 系统 | 每个数字应能指回具体后端字段；不得由前端估算、推断或加权合成 | 每个数字可核验 | BR-002 |

### Architecture 页

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-09 | 进入 Architecture 页时 | 系统 | 应渲染模块节点图：节点为 `modules` 的成员，边为 `dependency_graph` 的跨模块依赖方向 | 依赖拓扑可视 | `static_analysis/models.py:145-154` |
| R-10 | 用户点击某个节点时 | 用户 | 系统应在侧栏显示该模块的：名称、成员文件清单、内部边数、外部边数、聚类来历、该模块的分析结论原文 | 节点详情可读 | `Module` 与 `ModuleAnalysis` 字段 |
| R-11 | 渲染节点详情时 | 系统 | 「职责」应直接呈现 `ModuleAnalysis.summary` 原文，不得由前端重新概括；`limitation` 非空时应标注该分析不完整 | 结论不被二次加工 | `graph/state.py:36-47` |
| R-12 | 用户点击节点详情里的文件路径时 | 用户 | 系统应跳转到代码查看器并定位到该文件 | 从拓扑可下钻到代码 | D4 |
| R-13 | 依赖图降级为目录级粒度时 | 系统 | 应在图上显式标注降级及原因，不得静默以粗粒度呈现 | 粒度降级可见 | `DependencyGraph.granularity` |
| R-14 | 渲染 Architecture 页时 | 系统 | 不得渲染函数级调用链或调用关系边 | 界面不含无数据源的调用链 | `README.md:130-132` |

### 代码查看器

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-15 | 进入代码查看器时 | 系统 | 应渲染三栏：文件树、代码正文、该文件的评审发现 | 三栏布局 | D4 预览 |
| R-16 | 用户点击文件树某文件时 | 用户 | 系统应请求该文件内容并渲染，带行号 | 代码可读且行号可对照 | 需新增文件内容端点 |
| R-17 | 请求文件内容时 | 系统 | 应复用 `resolve_within` 做路径校验，拒绝仓库外路径与符号链接/junction | 路径逃逸被拒 | `backend/paths.py`（KTD14/KTD15） |
| R-18 | 文件超过体积上限时 | 系统 | 应返回前 N 行并标注「已截断，共 M 行」，不得静默截断 | 截断可见 | `max_file_bytes` 现有语义 |
| R-19 | 打开某文件时 | 系统 | 右栏应显示该文件已有的评审发现（path 匹配），每条含行号、严重度、message、evidence | 结论与代码同屏 | D9、`review/models.py` |
| R-20 | 打开的文件没有评审发现时 | 系统 | 右栏应显示「本文件无评审发现」，并区分「该文件不在评审目标范围内」与「在范围内但零命中」 | 零命中与未覆盖可区分 | plan R17 |
| R-21 | 渲染右栏时 | 系统 | 不得为当前文件发起新的 LLM 调用；右栏内容只来自已有评审发现 | 无额外成本、无新增幻觉面 | D9 |
| R-22 | 评审发现的 `line` 为 0 时 | 系统 | 应呈现为「属于文件整体」而非跳转到第 0 行 | 文件级问题可解释 | `review/models.py`（Finding 注释） |
| R-23 | 用户从报告或评审页点击某条引用时 | 用户 | 系统应跳转到代码查看器并定位到该路径与行号 | 引用可点进去核验 | D4 |

### Code Search 与 AI Chat

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-24 | 用户在 Code Search 页提交查询时 | 用户 | 系统应做向量语义检索并返回命中块的路径、行范围、符号名、代码片段 | 语义找代码 | 复用 `mcp_server/server.py:262-302` |
| R-25 | 该仓库尚未建立向量索引时 | 系统 | 应明确说明「未建索引」而非「未找到」，两者不得混同 | 两种情况可区分 | confirmed-source: `mcp_server/server.py:285-292` |
| R-26 | 用户点击检索结果时 | 用户 | 系统应跳转到代码查看器并定位到该行范围 | 检索结果可下钻 | D4 |
| R-27 | 用户在 AI Chat 页提问时 | 用户 | 系统应给出回答并附引用（路径 + 起止行 + 符号名），未找到时明确说明且不显示引用区 | 回答带可核验引用 | confirmed-source: `backend/rag/qa.py:83-95` |
| R-28 | 渲染 AI Chat 的引用时 | 系统 | 每条引用应可点击跳转到代码查看器对应位置 | 引用可核验 | D4 |
| R-29 | 用户连续提问时 | 系统 | 每次提问独立处理，不保留上下文，且界面应说明这一点 | 单轮语义不被误解为多轮 | confirmed-source: `README.md:138` |

### 三个评审页与 Reports 页

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-30 | 进入 Security Review / Error Handling / Structural 任一页时 | 系统 | 应只显示该类别的发现，按严重度分组并显示各组计数 | 按类别分页查看 | `FindingCategory` 三类 |
| R-31 | 某类检查未执行时 | 系统 | 对应页应显示未执行及原因，不得显示为「零发现」 | 未执行与零命中可区分 | plan R17、`CategoryOutcome.status` |
| R-32 | 某类检查已执行且零命中时 | 系统 | 应显示「已执行，零命中」并给出该类检查的覆盖范围说明 | 零命中可解释 | `CategoryOutcome.scope` |
| R-33 | 用户点击某条发现时 | 用户 | 系统应跳转到代码查看器并定位到该 path 与 line | 发现可下钻 | D4 |
| R-34 | 进入 Reports 页时 | 系统 | 应显示：架构报告全文（五节带引用）、评审执行情况表、引用校验通过率、缺失部分说明 | 报告完整可读 | `ReportModel` 字段 |
| R-35 | 渲染 Reports 页时 | 系统 | 不得显示架构评分、代码质量分、安全分或任何 0-100 的合成分数 | 界面不含编造的评分 | BR-002 |
| R-36 | 报告存在无法核验的结论时 | 系统 | 应显式呈现 `unsupported_claims` 与被丢弃结论数，不得隐藏 | 报告的不完整之处可见 | `ReportModel.unsupported_claims` |
| R-37 | 渲染报告正文时 | 系统 | 引用路径与行号应用等宽字体完整呈现，不截断；超宽时容器横向滚动 | 引用可核验 | 继承 002 的 R-09 |

### 报告导出

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-38 | 用户在 Reports 页选择导出格式时 | 用户 | 系统应支持 Markdown、HTML、PDF 三种格式导出当次分析的报告与评审发现 | 报告可带走 | D4 |
| R-39 | 生成导出内容时 | 系统 | 导出内容应包含引用路径与行号、评审执行情况、缺失说明，与界面呈现的口径一致 | 导出与界面不矛盾 | BR-003 |
| R-40 | PDF 生成失败时 | 系统 | 应明确报错并提示可改用 Markdown 或 HTML，不得返回损坏文件 | 失败可解释且有替代 | AE-14 |

### 首页与最近分析

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-41 | 打开首页时 | 系统 | 应显示仓库地址输入框、开始分析按钮，以及「最近分析」列表 | 首页即入口 | D5 |
| R-42 | 渲染「最近分析」列表时 | 系统 | 每条应显示仓库标识、commit 短 SHA、分析时间、文件数与发现数；不得显示星级评分 | 历史可读且无编造评分 | D2、D7 |
| R-43 | 分析完成时 | 系统 | 应将该次 `AnalysisResult` 落盘为 JSON | 重启后历史仍可读 | D7 |
| R-44 | 服务启动时 | 系统 | 应扫描落盘目录重建「最近分析」列表；单个文件损坏时跳过该条并计数，不影响整个列表 | 部分损坏不致列表不可用 | D7 |
| R-45 | 用户点击「最近分析」某条时 | 用户 | 系统应载入该次分析结果并进入 Overview 页 | 历史结果可重新浏览 | D7 |
| R-46 | 未完成的分析任务在服务重启后 | 系统 | 不应出现在「最近分析」列表中；界面不得声称它可恢复 | 不可恢复的任务不误导 | confirmed-source: `README.md:141-143` |

### 设置页与凭证边界

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-47 | 用户进入设置页时 | 用户 | 系统应允许配置 LLM base_url、API key、flash 档模型名、pro 档模型名 | 访客自带凭证 | D1 |
| R-48 | 存储访客凭证时 | 系统 | 应只存在访客浏览器本地，不得发送到服务端持久化、不得写入服务端日志 | 服务端无凭证残留 | D1、BR-004 |
| R-49 | 访客未配置凭证即提交分析时 | 系统 | 应拒绝提交并引导至设置页；不得回落使用服务端配置的凭证 | 不消耗他人额度 | D1、BR-004 |
| R-50 | 访客配置的凭证无效时 | 系统 | 应明确提示是访客自己配置的凭证问题，并指向设置页 | 失败归因清晰 | AE-13 |
| R-51 | 渲染设置页时 | 系统 | API key 输入框应默认掩码显示，且不得在任何界面文本、日志或导出内容中回显完整 key | 凭证不被意外暴露 | BR-004 |
| R-52 | 用户清除设置时 | 用户 | 系统应从浏览器本地移除凭证 | 凭证可撤回 | D1 |
| R-67 | 渲染设置页时 | 系统 | 不得出现 embedding provider、embedding key 或 embedding 模型名的配置项；应说明向量索引由服务端统一构建 | 访客只配 LLM 凭证 | D10 |
| R-68 | 访客未配置 LLM 凭证即提交分析时 | 系统 | 拦截应发生在提交阶段，不得先执行索引链路再失败 | 未配置凭证不消耗 owner 的 embedding 额度 | D10、`backend/graph/builder.py:166`（索引分支不依赖 LLM） |

### 公网部署与资源保护

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-53 | 同一 IP 在时间窗内提交分析次数超过上限时 | 系统 | 应拒绝新提交并返回明确的限流说明与可重试时间 | 限流可解释 | D6 |
| R-54 | 同时在跑的分析任务数达到上限时 | 系统 | 应排队而非拒绝，并向用户显示排队位置 | 排队可见 | D6 |
| R-55 | 仓库工作副本总量达到磁盘配额时 | 系统 | 应按最近最少使用清理，且不得清理正在分析的仓库 | 磁盘不被耗尽 | D6 |
| R-56 | 请求的仓库超出现有准入门限时 | 系统 | 应沿用现有准入拒绝逻辑并说明原因 | 超规模仓库被拒 | confirmed-source: `backend/ingest/admission.py` |
| R-57 | 部署到公网时 | 系统 | 文件读取端点必须限制在已分析仓库的工作副本内，不得暴露服务器其它路径 | 无任意文件读取 | R-17、KTD14 |
| R-69 | 克隆因瞬时网络故障（TLS 断连、DNS 失败、连接重置等）失败时 | 系统 | 应退还该提交方本次占用的限流配额，用户可立即重新提交而不必等到时间窗滑过 | 「这次失败不占用限流配额」 | confirmed-source: `backend/api/ratelimit.py:112`、`backend/api/tasks.py:190`、`backend/api/routes.py:172`；D11 |
| R-70 | 克隆失败且失败特征属瞬时网络类时 | 系统 | 应自动重试（退避后重试，上限固定）；重试仍失败才呈现为失败，且说明已重试过 | 抖动被自动吸收，不必人工重试 | confirmed-source: `backend/ingest/clone.py:117-142`、`tests/test_ingest_clone_retry.py`；D11 |

**R-53 的计数语义随 R-69 修订。** R-53 的「同一 IP 在时间窗内提交分析次数超过上限」按 R-69 读作「超过上限的**非网络失败**提交」——因我们这侧网络故障而失败的提交不计入。这与「未配置凭证不消耗限流计数」是同一条原则：没有产生有效分析的提交不扣配额，区别只是凭证那道门在计数之前、克隆失败发生在计数之后，所以只能退。退还次数不设上限，取舍见 Evidence And Assumptions。

### MCP 页

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-58 | 进入 MCP 页时 | 系统 | 应显示 7 个工具的名称与用途说明，以及 Cursor / Claude Code 的配置片段 | 接入方式可复制 | `mcp_server/server.py:82-262` |
| R-59 | 渲染 MCP 页时 | 系统 | 不得显示 MCP server 的运行状态灯或连接状态 | 界面不含不可观测的状态 | confirmed-source: `README.md:95-96` |
| R-60 | 渲染 MCP 页时 | 系统 | 应说明 MCP 工具读取的是已分析仓库的本地数据，需先完成一次分析 | 前置条件可读 | confirmed-source: `README.md:112` |

### 视觉与可访问性

| 编号 | 触发条件 | 角色 | 系统行为 | 用户可见结果 | 证据 / 约束引用 |
| --- | --- | --- | --- | --- | --- |
| R-61 | 渲染任何界面元素时 | 系统 | 应沿用 `#303d67` / `#79799a` / `#fddfdc` 派生的深色令牌体系 | 配色统一 | confirmed-source: `styles.css:12-37` |
| R-62 | 渲染页面背景时 | 系统 | 应以该三色渐变作为底色 | 渐变作为底色 | D8（用户明确要求） |
| R-63 | 渲染正文时 | 系统 | 渐变底色不得影响正文与代码的可读对比度 | 长文可读 | BR-005 |
| R-64 | 用户使用键盘操作时 | 用户 | 导航项、输入框、按钮、文件树节点、图节点应可 Tab 到达且焦点态可见 | 键盘可用 | plan U13 可访问交互要求 |
| R-65 | 页面切换后 | 系统 | 焦点应移至新页主标题，且切换应被读屏播报 | 读屏可跟随 | plan U13 可访问交互要求 |
| R-66 | 渲染模块节点图时 | 系统 | 应同时提供等价的文本形式（模块清单与依赖关系表），不得只有图形一种表达 | 图形不可读时仍可用 | BR-006 |

业务规则：

- BR-001：**导航形态改为多页切换，废除 002 的 BR-003。** 既有 `App.test.tsx` 三组 describe 依赖「完成态下报告/评审/问答三个 `h2` 共存」，多页切换后任一时刻只有一页在 DOM，这些测试必须改写为「先切页 → 再断言」。允许改写测试写法，不允许删除断言或降低覆盖。
- BR-002：**不引入任何后端无数据源的数字。** 禁止出现架构评分、代码质量分、安全分、技术债估时、Agent 星级、Stars、代码行数。所有展示数字必须能指回具体后端字段。
- BR-003：导出内容与界面呈现的口径必须一致。同一份分析在界面上说「25/26 条引用通过校验」，导出里不得是别的数字。
- BR-004：**服务端不得持久化访客 LLM 凭证。** 不落盘、不写日志、不进错误上报。访客未配置时不得回落使用服务端 `.env` 的凭证。
- BR-005：渐变底色不得压过正文与代码可读性。正文与代码区域需保证足够对比度。
- BR-006：模块节点图必须有等价文本表达。图形是增强而非唯一通道。
- BR-007：新增的文件读取端点必须复用 `resolve_within`，不得放宽路径校验（KTD14/KTD15）。
- BR-008：分析流水线的八个节点、SSE 生命周期、引用校验链路本次不动。

优先级分级：

| 编号 | 优先级 | 可降级方案 | 是否阻塞上线 |
| --- | --- | --- | --- |
| R-01 ~ R-02 | P0 / Must | 无（多页外壳是本期本体） | 是 |
| R-03 | P0 / Must | 降级为进入后显示空态 | 是 |
| R-04 | P1 / Should | 降级为窄视口下横向滚动 | 否 |
| R-05 | P0 / Must | 不可降级（公网下失效链接必然出现） | 是 |
| R-06 | P0 / Must | 降级为只显示文件数与发现数 | 是 |
| R-07 ~ R-08 | P0 / Must | 不可降级（BR-002 是本期核心取舍） | 是 |
| R-09 ~ R-10 | P0 / Must | 降级为依赖关系表（见 R-66） | 是 |
| R-11 | P0 / Must | 不可降级（结论不得二次加工） | 是 |
| R-12 | P1 / Should | 降级为显示路径不可点 | 否 |
| R-13 | P0 / Must | 不可降级（降级必须可见） | 是 |
| R-14 | P0 / Must | 不可降级（无数据源） | 是 |
| R-15 ~ R-17 | P0 / Must | 不可降级（查看器是本期核心增量，R-17 是安全底线） | 是 |
| R-18 | P0 / Must | 不可降级（静默截断会误导） | 是 |
| R-19 ~ R-20 | P0 / Must | 降级为只列发现不高亮行 | 是 |
| R-21 | P0 / Must | 不可降级（D9 决定） | 是 |
| R-22 | P1 / Should | 降级为不显示行号 | 否 |
| R-23 | P0 / Must | 降级为手动在查看器里找 | 是 |
| R-24 ~ R-25 | P0 / Must | 不可降级（R-25 的区分是已签需求） | 是 |
| R-26 | P1 / Should | 降级为显示路径不可点 | 否 |
| R-27 | P0 / Must | 不可降级（现有能力，不得退化） | 是 |
| R-28 | P1 / Should | 降级为不可点 | 否 |
| R-29 | P1 / Should | 降级为不作说明 | 否 |
| R-30 ~ R-32 | P0 / Must | 降级为三类合并一页 | 是 |
| R-33 | P1 / Should | 降级为不可点 | 否 |
| R-34 | P0 / Must | 无 | 是 |
| R-35 ~ R-36 | P0 / Must | 不可降级（BR-002 与报告完整性） | 是 |
| R-37 | P0 / Must | 不可降级（可核验性底线） | 是 |
| R-38 | P1 / Should | 降级为只做 Markdown | 否 |
| R-39 | P0 / Must | 不可降级（BR-003） | 是（若做导出） |
| R-40 | P1 / Should | 降级为不提供 PDF | 否 |
| R-41 | P0 / Must | 无 | 是 |
| R-42 | P0 / Must | 降级为只显示仓库名与时间 | 是 |
| R-43 ~ R-45 | P1 / Should | 降级为不做持久化，重启后列表为空 | 否 |
| R-46 | P0 / Must | 不可降级（不得误导可恢复） | 是 |
| R-47 ~ R-49 | P0 / Must | 不可降级（公网部署前提） | 是 |
| R-50 | P0 / Must | 降级为通用错误提示 | 是 |
| R-51 | P0 / Must | 不可降级（凭证暴露不可接受） | 是 |
| R-52 | P1 / Should | 降级为手动清浏览器数据 | 否 |
| R-67 | P0 / Must | 不可降级（D10 决定，暴露 embedding 配置会让索引键分裂） | 是 |
| R-68 | P0 / Must | 不可降级（否则未配置凭证仍消耗 owner 额度） | 是 |
| R-53 ~ R-55 | P0 / Must | 不可降级（D6 决定，公网资源保护） | 是 |
| R-69 | P1 / Should | 降级为不退还——用户须等时间窗滑过（D11 决定，非公网上线前提） | 否 |
| R-70 | P1 / Should | 降级为不自动重试，网络抖动直接呈现为失败 | 否 |
| R-56 | P0 / Must | 不可降级（现有能力） | 是 |
| R-57 | P0 / Must | 不可降级（任意文件读取是严重漏洞） | 是 |
| R-58 ~ R-60 | P1 / Should | 降级为纯文档链接 | 否 |
| R-61 | P0 / Must | 无（沿用既有令牌） | 是 |
| R-62 | P1 / Should | 降级为纯色底 | 否 |
| R-63 | P0 / Must | 不可降级（可读性优先于观感） | 是 |
| R-64 ~ R-65 | P0 / Must | 不可降级（plan U13 已签） | 是 |
| R-66 | P0 / Must | 不可降级（BR-006） | 是 |

## Acceptance Examples

```text
AE-01（对应 R-01、R-02、R-03）多页导航与直达
  Given 一次已完成的分析
  When 用户点击左侧导航的 Architecture
  Then 主内容区渲染模块节点图，Overview 页内容从 DOM 卸载
  And URL 变为该页地址
  When 用户刷新浏览器
  Then 仍停留在 Architecture 页且节点图重新渲染
  When 分析尚未就绪时用户查看导航
  Then Architecture / AI Chat / Code Search / 三个评审页 / Reports 均不可点
  And 每项标注「需先完成一次分析」
```

```text
AE-02（对应 R-06、R-07、R-08）概览口径
  Given 一次对 33 个文件、9 个模块、6 条发现、99 个索引块的分析
  When 用户查看顶部概览条
  Then 显示 文件 33 / 模块 9 / 发现 6 / 切块 99 与语言分布 Python 26
  And 不出现 Stars、代码行数、技术债、星级或任何 0-100 的分数
  And 每个数字对应 AnalysisResult 里的一个具体字段
```

```text
AE-03（对应 R-09、R-10、R-11、R-12）节点图与下钻
  Given 分析产出了 9 个模块与跨模块依赖边
  When 用户进入 Architecture 页
  Then 渲染 9 个节点及其依赖方向
  When 用户点击 code/model/modules 节点
  Then 侧栏显示该模块的 6 个成员文件、内部边数、外部边数、聚类来历
  And 显示该模块分析结论的原文，不是前端重新概括的版本
  When 该模块分析带有 limitation
  Then 侧栏标注「该模块分析不完整」及原因
  When 用户点击成员文件 code/model/modules/deconv.py
  Then 跳转到代码查看器并打开该文件
```

```text
AE-04（对应 R-13、R-14、R-66）图的降级与边界
  Given 某仓库文件数超出 max_graph_nodes 导致依赖图降级为目录级
  When 用户进入 Architecture 页
  Then 图上显式标注「依赖图降级为目录级粒度」及原因
  And 图中不出现任何函数级调用关系边
  And 页面同时提供模块清单与依赖关系表的文本形式
```

```text
AE-05（对应 R-15、R-16、R-19、R-20、R-21）查看器三栏与右栏来源
  Given 评审在 backend/review/security.py 上产出了 2 条发现
  When 用户在代码查看器打开该文件
  Then 左栏显示文件树，中栏显示带行号的代码，右栏显示这 2 条发现
  And 每条发现显示行号、严重度、message 与 evidence
  And 打开该文件的过程中不产生任何 LLM 调用
  When 用户打开一个在评审目标范围内但零命中的文件
  Then 右栏显示「已评审，本文件无发现」
  When 用户打开一个不在评审目标范围内的文件
  Then 右栏显示「本文件不在本次评审目标范围内」
```

```text
AE-06（对应 R-17、R-18、R-57）查看器的安全与截断
  Given 用户构造路径 ../../../etc/passwd 请求文件内容
  When 后端处理该请求
  Then 请求被拒绝且不返回任何文件内容
  Given 仓库内存在指向仓库外的符号链接或 junction
  When 用户请求该路径
  Then 请求被拒绝
  Given 某文件超出 max_file_bytes
  When 用户打开它
  Then 返回前 N 行并显示「已截断，共 M 行」
```

```text
AE-07（对应 R-23、R-33、R-26、R-28）引用可点进去核验
  Given 报告某条结论引用 code/model/modules/deconv.py:45-78
  When 用户点击该引用
  Then 跳转到代码查看器，打开该文件并定位到第 45 行
  Given Security Review 页某条发现在 config.py 第 24 行
  When 用户点击该条发现
  Then 跳转到查看器并定位到 config.py 第 24 行
  Given 某条评审发现的 line 为 0
  When 用户点击它
  Then 打开该文件并标注「该问题属于文件整体」，不跳转到第 0 行
```

```text
AE-08（对应 R-24、R-25）检索与未建索引的区分
  Given 某仓库已完成分析且索引就绪
  When 用户在 Code Search 提交「认证逻辑在哪里实现」
  Then 返回命中块的路径、行范围、符号名与代码片段
  Given 某仓库的向量索引不存在或索引键不匹配
  When 用户提交查询
  Then 显示「该仓库尚未建立向量索引，无法语义检索」
  And 不显示为「未检索到相关代码」
```

```text
AE-09（对应 R-30、R-31、R-32）零命中与未执行可区分
  Given 结构类检查已执行且零命中，安全类检查因故未执行
  When 用户进入 Structural 页
  Then 显示「已执行，零命中」并给出该类检查的覆盖范围说明
  When 用户进入 Security Review 页
  Then 显示「未执行」及原因
  And 两页的呈现不得都表现为空列表
```

```text
AE-10（对应 R-34、R-35、R-36）报告页口径
  Given 一次分析产出 26 条结论，其中 25 条引用完全有效、1 条部分无效
  When 用户进入 Reports 页
  Then 显示架构报告五节全文、评审执行情况表、引用校验通过率 25/26
  And 显示缺失部分说明（未解析文件数与原因分布）
  And 不出现架构评分、代码质量分、安全分
  Given 报告存在被丢弃的无法核验结论
  When 用户查看 Reports 页
  Then 被丢弃结论数与说明显式呈现
```

```text
AE-11（对应 R-41、R-42、R-45、R-46）首页与历史
  Given 服务端已落盘 3 次历史分析
  When 用户打开首页
  Then 显示仓库输入框与「最近分析」3 条
  And 每条显示仓库标识、commit 短 SHA、时间、文件数、发现数
  And 不显示星级评分
  When 用户点击其中一条
  Then 载入该次结果并进入 Overview 页
  Given 服务重启前有一个未完成的分析任务
  When 服务重启后用户打开首页
  Then 该任务不出现在列表中，界面不声称它可恢复
```

```text
AE-12（对应 R-43、R-44）落盘与部分损坏
  Given 一次分析完成
  When 结果落盘后服务重启
  Then 该次分析出现在「最近分析」列表且可载入
  Given 落盘目录中有 1 个 JSON 文件损坏、4 个正常
  When 服务启动扫描该目录
  Then 列表显示 4 条正常记录
  And 损坏的那条被跳过并计数，不导致列表整体不可用
```

```text
AE-13（对应 R-47、R-48、R-49、R-50）访客凭证边界
  Given 访客首次访问且未配置 LLM 凭证
  When 访客提交一个仓库地址
  Then 提交被拒绝并引导至设置页
  And 后端未使用服务端 .env 中的凭证发起任何 LLM 调用
  Given 访客在设置页填入了无效的 API key
  When 分析执行到需要 LLM 的节点
  Then 提示「你配置的 LLM 凭证无效」并指向设置页
  And 服务端日志中不出现该 key 的完整值
  And 服务端落盘文件中不出现该 key
```

```text
AE-14（对应 R-38、R-39、R-40）导出
  Given 一次已完成的分析，界面显示引用校验通过率 25/26
  When 用户导出 Markdown
  Then 导出内容包含报告五节、引用路径与行号、评审执行情况、缺失说明
  And 导出中的引用校验通过率同样是 25/26
  When 用户导出 PDF 而生成失败
  Then 显示明确错误并提示可改用 Markdown 或 HTML
  And 不产生一个损坏的 PDF 文件
```

```text
AE-15（对应 R-53、R-54、R-55）公网资源保护
  Given IP 限流上限为每小时 3 次
  When 同一 IP 第 4 次提交分析
  Then 请求被拒绝并显示限流说明与可重试时间
  Given 并发上限为 2 个任务
  When 第 3 个任务提交
  Then 该任务进入排队并显示「前面还有 N 个任务」
  Given 仓库工作副本总量达到磁盘配额
  When 新的分析需要克隆
  Then 按最近最少使用清理旧副本
  And 正在分析中的仓库不被清理
```

```text
AE-16（对应 R-58、R-59、R-60）MCP 页
  Given 一次已完成的分析，MCP server 由客户端自行拉起且后端无法观测其状态
  When 用户进入 MCP 页
  Then 显示 7 个工具的名称与用途，以及 Cursor / Claude Code 的配置片段
  And 不显示运行状态灯或连接状态
  And 说明工具读取的是已分析仓库的本地数据，需先完成一次分析
```

```text
AE-17（对应 R-61、R-62、R-63、R-64、R-65、R-66）视觉与可访问
  Given 工作台已加载且沿用 styles.css 的深蓝柔粉令牌
  When 用户查看任意页面
  Then 背景为 #303d67 → #79799a → #fddfdc 渐变
  And 正文与代码区域的对比度不因渐变而下降到不可读
  When 用户用键盘 Tab 遍历
  Then 导航项、输入框、按钮、文件树节点、图节点均可到达且焦点态可见
  When 用户切换页面
  Then 焦点移至新页主标题且切换被读屏播报
```

```text
AE-18（对应 R-05）失效链接
  Given 某次分析的落盘文件已被磁盘配额清理
  When 用户通过旧 URL 直接访问该分析
  Then 显示「该分析不存在或已被清理」与返回首页入口
  And 不显示空白页或未处理的错误堆栈
```

```text
AE-19（对应 R-04、R-37）窄视口与引用不截断
  Given 一份含长引用路径 backend/static_analysis/entrypoints.py:128-169 的报告
  When 用户把视口缩到 900px 宽
  Then 左侧导航转为顶部横向或抽屉式，主内容区占满宽度且页面不横向溢出
  And 引用路径与行号仍用等宽字体完整呈现，不截断也不移入悬浮层
  And 该引用超宽时由其容器横向滚动，不撑破整页布局
```

```text
AE-20（对应 R-27、R-29）问答的引用与单轮语义
  Given 某仓库索引就绪
  When 用户在 AI Chat 提问「为什么这里用 Redis」而检索无相关内容
  Then 明确说明未找到，且不显示引用区
  When 用户改问一个能检索到的问题
  Then 回答附带引用，每条含路径、起止行与符号名
  And 界面说明每次提问独立处理、不保留上下文
```

```text
AE-21（对应 R-51、R-52）凭证不回显与可撤回
  Given 访客已在设置页填入 API key
  When 访客重新打开设置页
  Then key 输入框默认掩码显示，完整值不出现在界面文本中
  And 该 key 的完整值不出现在任何导出内容里
  When 访客点击清除设置
  Then 凭证从浏览器本地移除，再次提交分析时按未配置处理
```

```text
AE-23（对应 R-67、R-68）嵌入凭证不开放且拦截在提交阶段
  Given 嵌入凭证由服务端持有，访客只应配置 LLM 凭证
  When 访客打开设置页
  Then 页面只出现 LLM base_url、key、两个模型名四项
  And 不出现 embedding provider、embedding key 或 embedding 模型名任何配置项
  And 页面说明向量索引由服务端统一构建
  Given 访客未配置 LLM 凭证
  When 访客提交一个仓库地址
  Then 提交在入口即被拒绝并引导至设置页
  And 后端未执行 clone、未执行解析、未调用 embedding provider
  And owner 的 embedding 额度消耗为零
```

```text
AE-22（对应 R-22、R-56）文件级问题与超规模仓库
  Given 评审产出一条循环依赖发现，其 line 为 0
  When 用户在查看器打开该文件
  Then 右栏标注该问题属于文件整体，不跳转到第 0 行
  Given 某仓库可解析文件数超过 max_parseable_files
  When 用户提交该仓库
  Then 沿用现有准入拒绝逻辑并说明超限原因
  And 不产生克隆残留占用磁盘配额
```

```text
AE-24（对应 R-69、R-70、R-53）网络类失败的自动重试与配额退还
  Given 某 IP 的限流上限为 3 次/小时，该 IP 已用掉 1 次
  When 该次提交的克隆因 TLS 断连失败，且重试到上限仍未成功
  Then 界面说明分析未能完成、已自动重试过，且这次失败不占用限流配额
  And 该 IP 的已用计数回到 0，可立即再次提交
  Given 克隆首次失败的输出属瞬时网络特征
  When 系统按退避重试
  Then 重试成功时分析照常继续，用户看不到失败
  Given 克隆失败的输出是「仓库不存在」或「认证失败」
  When 系统判定该失败为永久性
  Then 不重试、不退还配额，并按原因呈现为仓库不存在或无权限
  Given 浏览器无法连接后端（请求未到达服务端）
  When 界面呈现该失败
  Then 提示指向「确认后端已启动」，不声称已自动重试，也不提配额
```

## Negative Acceptance

以下情形必须**不**发生。它们对应本期最容易被侵蚀的边界：

```text
NA-01（BR-002）界面任何位置不得出现：架构评分、代码质量分、安全分、
      技术债估时、Agent 星级、Stars 数、代码总行数。
      判定：全站文本搜索这些标签应无命中。

NA-02（BR-004）服务端不得持久化访客凭证。
      判定：访客配置并触发一次分析后，服务端日志、.workspace 落盘文件、
      错误上报中均不含该 key 的完整值。

NA-03（R-49）访客未配置凭证时，不得回落使用服务端凭证。
      判定：清空访客配置后提交分析，服务端不产生任何 LLM 请求。

NA-04（R-17、R-57）文件读取端点不得返回工作副本之外的内容。
      判定：路径穿越、绝对路径、符号链接、junction 四类输入全部被拒。

NA-05（R-14）不得渲染函数级调用链。
      判定：Architecture 页不存在「调用」语义的边或子视图。

NA-06（R-59）不得显示 MCP 运行状态。
      判定：MCP 页无状态灯、无「Running」字样。

NA-07（R-21）打开文件不得触发 LLM 调用。
      判定：连续打开 10 个文件，LLM 请求计数不变。

NA-08（BR-001）改写测试不得降低覆盖。
      判定：改写后测试数不少于 53，且原有断言语义保留。

NA-09（R-31、R-32、R-20）零命中与未执行不得混同呈现。
      判定：两种状态的界面文案不同且都非空。

NA-10（R-46）不得声称未完成任务可恢复。
      判定：重启后界面无「继续」「恢复」类入口指向已丢失的任务。

NA-11（R-69）业务类失败不得退还限流配额。
      判定：准入拒绝、凭证无效、LLM 调用失败三类失败后，该 IP 的
      已用配额计数不变——退还只对网络类失败成立，否则限流形同虚设。

NA-12（R-70）浏览器侧连接失败不得呈现为「已自动重试且不占配额」。
      判定：后端不可达时的提示不含「重试」「配额」字样，且与
      克隆网络失败的提示文案不同。
```

## Scope Boundaries

### 本期做

- **前端 9 页多页形态**：Overview、Architecture、AI Chat、Code Search、Security Review、Error Handling、Structural、Reports、MCP，加设置页。客户端路由，URL 可直达可刷新。
- **模块节点图**：节点为模块，边为跨模块依赖，点击看详情，可下钻到文件。含等价文本表达。
- **代码查看器**：文件树 + 带行号代码 + 该文件已有评审发现，三栏。
- **报告导出**：Markdown、HTML、PDF 三种格式。
- **最近分析持久化**：`AnalysisResult` 落盘 JSON，启动时扫目录重建列表。
- **设置页**：访客配置自己的 LLM base_url / key / 模型名，存浏览器。
- **公网部署形态**：IP 限流、并发闸门、磁盘配额三项资源保护。
- **新增后端端点**：文件内容读取、代码搜索、报告导出、分析历史列表、单次历史结果读取。
- **改写现有前端测试**：53 个测试适配多页形态，改写法不删断言。

### 本期不做（Non-Goals）

- **函数级调用链与调用图**。解析层不提取调用点（`README.md:130-132`），`find_references` 是依赖图定界的文本匹配。要做需先补符号级引用分析，属独立需求。
- **Performance 类检查与 Performance 页**。后端 `FindingCategory` 只有三类，无性能检查器。新增一类需要检测器 + 判断 prompt + 基准验证，工作量与本期不相称。
- **通用 Bug 类检查**。同上。Bug Review 在本期落为 Error Handling 页（对应真实存在的 `error_handling` 类别）。
- **MCP 运行状态观测**。MCP 走 stdio 由客户端拉起子进程，后端架构上观测不到。要做需改为常驻服务加心跳，属架构变更。
- **架构评分 / 代码质量分 / 安全分 / 技术债估时 / Agent 星级**。后端无评分机制，且这类数字的权重无客观依据。owner 已选定改为可派生的真实计数（D2）。
- **Stars / 代码行数**。前者需调 GitHub API 额外字段，后者需解析层新增统计，均非本期核心价值。
- **多用户、账号体系、协作、分享**。本期是「无鉴权 + 访客自带 key」形态，不引入用户概念。
- **未完成任务的断点恢复**。任务状态仍在进程内存，重启即丢（`README.md:141-143`）。本期只做已完成结果的落盘。
- **服务端托管 LLM 凭证的多租户方案**。owner 已选定访客自带 key（D1）。
- **浅色主题与主题切换**。沿用 002 的决定。
- **私有仓库接入**。plan 已列为 Deferred。
- **多轮问答与 query 改写**。`README.md:138` 明列问答是单轮。

### 与其它模块/需求的关系

- **supersede** `docs/brainstorms/2026-08-25-002-ui-workbench-redesign-requirements.md`：该 PRD 的 BR-001/002/003/006 四条硬约束本次全部被推翻。其视觉令牌成果（`styles.css` 色阶体系）继续沿用。
- 依赖 `docs/plans/2026-08-23-001-feat-codepilot-agent-plan.md` 的 U12（FastAPI 后端与 SSE）与 U13（React 前端）：SSE 生命周期、失败四分类、可访问交互三项要求继承不变。
- **突破** 同 plan 的 R23「界面不承担 Agent 执行过程的可视化展示」的处理方式：本期不做执行过程可视化（Performance 页已去掉），故 R23 实质未被突破，但 plan 的「浏览式代码浏览器在产品定位之外」这一条被本期的代码查看器推翻（D4）。
- **突破** plan 的「单页应用，无路由」：本期改为客户端路由多页（D3）。
- 依赖 `backend/paths.py` 的 KTD14/KTD15：新增文件读取端点必须复用同一校验且不放宽。

## Evidence And Assumptions

| 主张 | 类型 | 证据来源 / 为何是假设 | 确认路径 |
| --- | --- | --- | --- |
| 现有前端单页无路由，完成态三个 h2 共存 | confirmed-source | `frontend/src/App.tsx:141-203` 已读 | source |
| 现有前端 53 个测试，8+2 个 describe | confirmed-source | 本次实测 `grep -c "it("` 与 `grep -n "describe("` | source |
| HTTP 层无文件读取端点、无搜索端点 | confirmed-source | 本次实测 `grep -rn` 于 `backend/api/` 无命中 | source |
| 后端无鉴权层，端口只绑 127.0.0.1 | confirmed-source | `README.md:48-49,140` | source |
| 任务状态在进程内存，重启即丢 | confirmed-source | `README.md:141-143`、`backend/api/tasks.py:86-100` | source |
| 评审只有三类，无 performance | confirmed-source | `backend/review/models.py:20-26` | source |
| 评审发现带 path + line + message + evidence，line=0 表示文件级 | confirmed-source | `backend/review/models.py`（Finding 类）已读 | source |
| `Symbol` 带 1-based 起止行号 | confirmed-source | `backend/static_analysis/models.py:21-35` | source |
| `ModuleAnalysis.summary` 是自由文本，无结构化「职责」字段 | confirmed-source | `backend/graph/state.py:36-47` | source |
| 解析层不提取调用点 | confirmed-source | `README.md:130-132` | source |
| 后端无任何评分机制 | confirmed-source | 本次实测 `grep -rn "score\|rating\|grade"` 仅命中 Planner 内部 `rule_score` | source |
| 无导出能力与相关依赖 | confirmed-source | 本次实测 `grep -rn "pdf\|export\|download" backend/` 无相关命中 | source |
| MCP 走 stdio 由客户端拉起，后端观测不到状态 | confirmed-source | `README.md:95-96` | source |
| `search_code` 已能区分「未建索引」与「未找到」 | confirmed-source | `backend/mcp_server/server.py:285-292` 已读 | source |
| 问答已返回带路径与行号的引用 | confirmed-source | `backend/rag/qa.py:83-95`（Citation 类）已读 | source |
| 配色三色值与色阶体系已落地 | confirmed-source | `frontend/src/styles.css:4-37` 已读 | source |
| `resolve_within` 已防符号链接与 junction 逃逸 | confirmed-source | `backend/paths.py` 已读（含 TOCTOU 说明） | source |
| 访客自带 key 是本期部署形态 | user-stated | D1：owner 选定「公网 + 访客自带 key，服务端不存凭证」 | 当前执行对话用户 |
| 不做评分，改为可派生真实计数 | user-stated | D2：owner 选定「换成可派生的真实计数」 | 当前执行对话用户 |
| 改为真多页切换并改写测试 | user-stated | D3：owner 选定「改为真多页切换，改写相关测试」 | 当前执行对话用户 |
| 四项新能力全部进本期 | user-stated | D4：owner 多选「架构图 + 代码查看器/搜索 + 导出 + 持久化」 | 当前执行对话用户 |
| 导航按真实能力重构为 9 项 | user-stated | D5：owner 选定「按真实能力重构导航」 | 当前执行对话用户 |
| IP 限流 + 并发闸门 + 磁盘配额 | user-stated | D6：owner 选定该项 | 当前执行对话用户 |
| 历史存 JSON 文件而非 SQLite | user-stated | D7：owner 选定「JSON 文件落盘」 | 当前执行对话用户 |
| 渐变作为页面底色 | user-stated | D8：用户原话「背景底色按我给你的渐变色为底色」 | 当前执行对话用户 |
| 查看器右栏只复用已有评审发现 | user-stated | D9：owner 选定「只复用已有评审发现」 | 当前执行对话用户 |
| IP 限流的具体阈值（每小时 3 次、并发 2） | assumption | 选项预览中的示例值，owner 选的是「做限流」这个形态而非具体数字 | 记入 Planning Recheck，实现时可调 |
| 磁盘配额的具体容量 | assumption | 未与 owner 确认具体 GB 数 | 记入 Planning Recheck |
| 嵌入模型凭证由服务端持有（owner 自己的 API），不开放给访客 | user-stated | D10：owner 明确「嵌入凭证用我的 api，不开放」；技术依据是索引键含 `provider_identity`，访客自带会让缓存分裂失效 | 当前执行对话用户 |
| 语言分布展示前 N 项 | assumption | `LanguageProfile.by_language` 是全量映射，界面需截断，N 未确认 | 记入 Planning Recheck |
| **R-62 与 R-63 靠分层共存，实测对比度已量化** | confirmed-source | 实现后实测（2026-08-26）：浅色正文 `--ink #e9ebf5` 在渐变三色上的对比度分别为深蓝 `#303d67` 8.88、过渡紫 `#79799a` 3.52、柔粉 `#fddfdc` 1.05 —— 只有深蓝端能直接承载文字。故内容列铺 88% 不透明托底层，面板色系从基调提亮（对 `--ink` 为 9.8–13.5:1，AA 需 4.5）。渐变在页边距与面板间隙完整裸露，满足 R-62 的「以该三色渐变作为底色」；文字始终落在托底或面板上，满足 R-63。实现期曾一度改用压暗后的渐变变体以求稳妥，owner 于本次实机核对时要求改回配色原三色（见 Owner Decision Trace 的 OQ-15 行）—— 压暗变体其实未满足 R-62 的字面要求（「**该**三色渐变」） | 已闭合：R-63 的可读性下限由实测数字支撑，不再只是设计判断 |
| **R-03 的置灰原因不做可见文案，视力正常的键盘用户取不到它** | user-stated | owner 于 2026-08-26 实机核对后要求去掉可见文案（见 Owner Decision Trace 的 OQ-13 行）。原因改挂 `title` 与 `aria-label`：鼠标悬停与读屏可读，而 disabled 按钮不可聚焦，故键盘 Tab 到不了、tooltip 不触发 | owner 已定，不再询问。若日后要补齐该人群，可行做法是把置灰项改为可聚焦但 `aria-disabled` 的按钮（Tab 可达、回车不响应），代价是引入一个「看起来能点但点不动」的控件 |
| **R-53 的按 IP 计数依赖反代覆写 `X-Forwarded-For`；直接暴露容器端口时该保证不成立** | confirmed-source | 实现后实测（2026-08-26，容器实跑）：`BIND_ADDR=0.0.0.0` 起服务后，从宿主局域网地址 `192.168.31.54` 发起的请求在后端被记录为 `172.20.0.1`（Docker 网桥网关）——所有外部客户端归一到同一标识，于是「每 IP 每小时 3 次」塌为全站共用一个配额。限流机制本身正确（XFF 存在时按最左项分别计数，由 `tests/test_ratelimit.py::TestClientKey` 与 `tests/test_api.py::TestRateLimit::test_forwarded_for_separates_clients` 覆盖），失守的是它的输入。R-53 原文未记录任何部署前提，Non-Goals 也未将部署拓扑划出，故此处补记为已测约束 owner 已定（plan KTD9，`session-settled: user-directed`）：反代必须透传真实客户端 IP，这是 R-53 的前提。本行是该前提的量化确认，非新增待决项；README 的公网部署一节据此把反代从「加固项」改述为「必要条件」 |

| **R-69 的退还不设次数上限，网络失败这一路径因此不受限流约束** | user-stated | owner 于 2026-08-26 选定「保留无上限退还」（见 Owner Decision Trace 的 OQ-16 行）。量化取舍：每次这样的失败仍消耗一次克隆尝试与 R-70 的 3 次重试退避（2s + 4s），但不落盘（`clone.py:217` 在失败路径 `rmtree`），所以不吃磁盘配额；R-54 的并发闸门仍按在跑任务数约束，所以并发上限不被绕过。真正不受约束的只有「重复发起注定失败的克隆」这一动作本身 | owner 已定，不再询问。若日后需要收紧，可行做法是给退还加窗口内次数上限（例如每小时最多退 3 次），代价是引入第二个需要解释的阈值 |

本需求不触及资金、交易、审计或合规。访客的 LLM API key 属敏感数据，边界写在 BR-004、R-48、R-51 与 NA-02。

## Interaction Requirements

| 元素 | 是否必须 | 展示/交互规则 | 文案 | 风险 / 约束 |
| --- | --- | --- | --- | --- |
| 左侧导航（9 项） | 是 | 点击切页并改 URL；结果未就绪时相关项置灰并标原因 | 见 R-01 的九项名称 | 必须多页切换，废除锚点（BR-001） |
| 顶部概览条 | 是 | 常驻；显示仓库、SHA、阶段与五项统计 | 未提交时「未选择仓库」 | 不得含无数据源数字（BR-002） |
| 模块节点图 | 是 | 节点可点，选中态可见；支持缩放与平移 | 降级时标注「依赖图降级为目录级粒度」 | 须有等价文本表达（BR-006） |
| 节点详情侧栏 | 是 | 显示模块字段与分析结论原文；文件路径可点 | `limitation` 非空时标「该模块分析不完整」 | 结论不得二次概括（R-11） |
| 文件树 | 是 | 目录可折叠；当前文件高亮 | 空仓库时「无可显示文件」 | 只列工作副本内文件（R-57） |
| 代码正文 | 是 | 带行号；跳转时目标行高亮 | 截断时「已截断，共 M 行」 | 不得静默截断（R-18） |
| 查看器右栏 | 是 | 列该文件发现，按行号排序；点击定位到行 | 三种空态文案见 R-20 | 不得触发 LLM（R-21、NA-07） |
| 检索结果列表 | 是 | 显示路径、行范围、符号名、代码片段 | 未建索引时见 R-25 文案 | 未建索引 ≠ 未找到（R-25） |
| 问答引用区 | 是 | 未找到时不显示引用区 | 沿用现有 `Answer.found` 语义 | 单轮语义须说明（R-29） |
| 导出按钮 | 否（P1） | 三种格式；生成中显示进度 | PDF 失败时见 R-40 文案 | 口径须与界面一致（BR-003） |
| 最近分析列表 | 否（P1） | 按时间倒序；显示五项元信息 | 空时「暂无历史分析」 | 不得显示星级（R-42） |
| 设置页 key 输入 | 是 | 默认掩码；可清除 | 未配置时提示见 R-49 | 不得回显完整 key（R-51） |
| 限流提示 | 是 | 显示可重试时间 | 「本 IP 每小时限 N 次，请稍后」 | 须给可重试时间（R-53） |
| 排队提示 | 是 | 显示排队位置 | 「前面还有 N 个任务」 | 排队而非拒绝（R-54） |
| MCP 配置片段 | 否（P1） | 可一键复制 | 见 R-60 的前置条件说明 | 不得有状态灯（R-59） |

## 路由、导航与浏览器行为

| 场景 | 目标行为 |
| --- | --- |
| 直接访问某页 URL | 载入该分析结果并渲染该页；分析不存在时见 R-05 |
| 浏览器后退 / 前进 | 在已访问过的页之间切换，不重新触发分析 |
| 刷新 | 保持当前页；重新拉取该分析结果 |
| 多标签页 | 各标签独立，共享同一份浏览器凭证配置 |
| 未配置凭证时访问任意页 | 可浏览已有历史分析结果；提交新分析时才拦截（R-49） |
| 分析进行中切换页面 | SSE 连接不中断，进度在 Overview 页继续更新 |
| 分析进行中刷新 | 重连 SSE；若任务已随重启丢失，显示不可恢复（R-46） |

## Exception Handling

| 场景 | 用户看到什么 | 是否可重试 | 降级结果 |
| --- | --- | --- | --- |
| 访客未配置凭证 | 引导至设置页 | 配置后可重试 | 仍可浏览历史分析 |
| 访客凭证无效 | 「你配置的 LLM 凭证无效」+ 设置页入口 | 改凭证后可重试 | 静态解析产物仍可看（不需 LLM 的部分） |
| IP 限流触发 | 限流说明 + 可重试时间 | 到时可重试 | 仍可浏览历史分析 |
| 并发已满 | 排队位置 | 自动排队 | 无需用户操作 |
| 磁盘配额触发 | 对用户透明（后台 LRU 清理） | — | 被清理的历史链接失效（R-05） |
| 仓库超出准入门限 | 沿用现有拒绝文案与原因 | 换仓库 | — |
| 克隆遇瞬时网络故障 | 用户无感（后台自动重试） | 自动重试，上限固定 | 重试成功则分析照常继续 |
| 自动重试后仍失败 | 「网络异常，且自动重试未成功」+ 说明本次不占限流配额 | 可立即重新提交 | 限流配额已退还（R-69） |
| 浏览器连不上后端 | 「无法连接后端」+ 提示确认后端已启动、地址与端口是否正确 | 后端可达后重试 | 与上一行必须可区分：请求未到后端，既无重试也无配额消耗（NA-12） |
| 文件读取路径非法 | 「无法读取该路径」 | 否 | 不暴露拒绝细节 |
| 文件过大 | 「已截断，共 M 行」 | — | 显示前 N 行 |
| 未建向量索引 | 「尚未建立向量索引」 | 重新分析后可用 | 结构类页面不受影响 |
| PDF 生成失败 | 明确错误 + 建议改用 MD/HTML | 可换格式 | MD/HTML 仍可导出 |
| 历史 JSON 单个损坏 | 该条不出现在列表 | — | 其余条目正常 |
| 分析中途某模块失败 | 报告缺失部分标注该模块及原因 | — | 其余模块分析仍产出 |
| SSE 断连 | 断连提示 + 重连 | 自动重连 | 沿用现有失败四分类 |

## Design Source Coverage

design_source_inventory:
- source_or_node: 用户本次提供的 ASCII 线框方案（首页 Dashboard、顶部项目概览、左侧导航 9 项、Architecture 节点图、代码查看器三栏、AI Chat 双栏、Code Review 分级、Reports 评分条与导出、MCP 状态页）
  read_status: read
  affected_prd_write_targets: Requirements | Acceptance Examples | Interaction Requirements | Scope Boundaries
  extracted_design_what: 多页工作台形态、9 项导航构成、三栏查看器布局、节点图加详情侧栏的交互、报告页结构、首页输入框加历史列表
  evidence_level: confirmed owner/source
  unread_or_degraded_reason: 无
  readiness_consequence: 布局与导航结构由 owner 提供的线框直接确定，无残留
  conflicts:
    - contradicts: 线框的「Reports 评分条 82/75/65」与后端无评分机制（`grep` 实测无 score 产出）
      owner_authority_needed: yes
      readiness_consequence: 已由 D2 闭合——owner 选定改为可派生真实计数，写入 R-07/R-35/NA-01
    - contradicts: 线框的「Performance 页」与 `FindingCategory` 只有三类
      owner_authority_needed: yes
      readiness_consequence: 已由 D5 闭合——去掉该页，Bug Review 改名 Error Handling，写入 Change Delta remove 行
    - contradicts: 线框的「Architecture 调用链」与解析层不提取调用点（`README.md:130-132`）
      owner_authority_needed: yes
      readiness_consequence: 已由 D5 闭合——去掉调用链子项，写入 R-14/NA-05
    - contradicts: 线框的「MCP 状态 🟢 Running」与 MCP 走 stdio 由客户端拉起
      owner_authority_needed: yes
      readiness_consequence: 已由 D5 闭合——改为配置说明页，写入 R-59/NA-06
    - contradicts: 线框的「用户登录」与本期无鉴权形态
      owner_authority_needed: yes
      readiness_consequence: 已由 D1 闭合——不做登录，改为访客自带 key，写入 R-47~R-52
    - contradicts: 线框的「最近分析 ⭐⭐⭐⭐⭐」与无评分机制
      owner_authority_needed: yes
      readiness_consequence: 已由 D2 闭合——改为显示文件数与发现数，写入 R-42
- source_or_node: 配色渐变参考图（`#303d67` / `#79799a` / `#fddfdc`，经 002 已落地为 `styles.css` 令牌）
  read_status: read
  affected_prd_write_targets: Requirements | Interaction Requirements
  extracted_design_what: 三色值及其角色（深蓝基调、灰紫过渡、柔粉辅助）；本次 owner 明确要求用作页面底色
  evidence_level: confirmed owner/source
  unread_or_degraded_reason: 无
  readiness_consequence: 令牌已存在于 `styles.css:12-37`，可直接复用；底色用法由 D8 确定
  conflicts:
    - contradicts: 002 的 R-07「渐变限用三处（品牌条、进度填充、统计卡发丝线）」
      owner_authority_needed: yes
      readiness_consequence: 已由 D8 闭合——owner 本次明确要求渐变作底色，推翻 002 的限制，写入 R-62；可读性下限由 R-63/BR-005 兜住

design_sources_read:
- 本次 ASCII 线框方案 → Requirements R-01~R-60、Acceptance Examples、Interaction Requirements，evidence_level: confirmed owner/source
- 配色渐变参考图 → Requirements R-61/R-62，evidence_level: confirmed owner/source

design_sources_unread:
- none

design_source_coverage: read
design_degraded_owner_acceptance_ref: none

说明：本需求没有 Figma 文件、组件规范或交互态设计稿。线框中六处与后端能力冲突的项已全部经 owner 裁定（D1/D2/D5/D8），裁定结果写入对应 R 项与 Negative Acceptance。间距尺度、圆角半径、字号阶梯、节点图布局算法、限流阈值的具体数值属实现细节，由 `spec-plan` 与实现阶段在 R-61~R-63 约束内决定。

## Decision Notes

- **D1 公网 + 访客自带 key，服务端不存凭证。** 用户原始方案含「用户登录」与「设置功能可自行配置 LLM URL 和 KEY」加「上线部署在服务器上」，三者叠加在无鉴权的现状下等于任意访客可读写服务端凭证。owner 选定访客自带 key：凭证只存访客浏览器，服务端不落盘不记日志，未配置时不回落服务端凭证。代价是仓库克隆仍是任意访客可触发，由 D6 的资源保护兜住。方案里的「用户登录」因此不做。
- **D2 不造评分，改为可派生的真实计数。** 方案要显示架构评分 82、代码质量 75、安全 65、技术债 25h、Agent 星级、Stars、代码行数。后端全无这些数据，评分权重也无客观依据——按项目「抗追问」的评判标准，编出来的分数是最容易被问倒的地方。owner 选定全部换成可指回字段的真实计数：文件数、模块数、发现数、切块数、语言分布、各类检查执行情况、引用校验通过率、缺失说明。这与项目已有的「引用由程序校验」卖点同构。
- **D3 改为真多页切换，废除 002 的 BR-003。** 方案要 9 个导航页，而 002 为了保住「完成态三个 h2 共存」选了锚点滚动，40 个测试依赖该语义。9 个页面里代码查看器、Code Search 这类有自己状态的页塞不进锚点模型。owner 选定改多页切换并改写测试——改写法不删断言（BR-001、NA-08）。
- **D4 四项新能力全部进本期。** 架构图可视化、代码查看器 + 搜索、报告导出、最近分析持久化。这推翻 002 的 BR-002（后端零改动）与 BR-006（不引入图表库），也推翻 plan 的「浏览式代码浏览器在产品定位之外」。新增 5 类后端端点。
- **D5 按真实能力重构导航，去掉三项无数据源的。** Performance 页去掉（`FindingCategory` 无此类，且真做需要检测器 + 判断 prompt + 基准验证）；Bug Review 改名 Error Handling（对应真实存在的类别）；调用链子项去掉（解析层不提取调用点）；MCP 状态灯去掉改为配置说明页（stdio 子进程后端观测不到）。导航仍是 9 项，但组成按真实能力重排。
- **D6 IP 限流 + 并发闸门 + 磁盘配额。** 访客自带 key 消掉了 LLM 额度风险，但克隆磁盘、向量化 CPU、带宽仍是服务器承担。owner 选定三项保护。具体阈值（每小时几次、并发几个、配额多少 GB）是选项预览里的示例值，实现时可调，记入 Planning Recheck。
- **D7 JSON 文件落盘而非 SQLite。** 首页「最近分析」需要持久化，而现在任务状态在进程内存重启即丢。owner 选定 JSON 文件——与已落盘的向量索引同一位置，不引入数据库依赖与 schema 演进责任。代价是无法按时间排序分页查询（列表规模小，可接受）。未完成任务仍不可恢复（R-46 明写不得误导）。
- **D8 渐变作页面底色，推翻 002 的 R-07。** 002 把渐变限制在三处（品牌条、进度填充、统计卡发丝线），理由是大面积渐变会压过正文可读性。owner 本次明确要求「背景底色按我给你的渐变色为底色」。按 owner-answer fidelity，该要求原样落为 R-62，不软化为「限用三处」；可读性下限单独由 R-63 与 BR-005 兜住，而非用来削弱 R-62。
- **D9 查看器右栏只复用已有评审发现，不实时调 LLM。** 方案的 Cursor 式体验倾向「打开文件就分析」。owner 选定只复用已有发现：零新增 LLM 调用、零新增幻觉面，且发现本身带 `evidence`（判断依据）比现场生成的结论更抗追问。代价是没有发现的文件右栏是空态（三种空态文案见 R-20）。
- **D10 嵌入凭证不开放给访客，用 owner 自己的 API。** owner 明确确认。技术依据：索引缓存键含 `provider_identity`（`backend/cache/key.py`），访客各自的 embedding provider 会让索引键分裂、缓存全部失效，同一仓库被反复向量化——与 D6 的资源保护直接冲突。后果是**向量化成本由 owner 承担而非访客**：设置页只暴露 LLM 凭证（R-47），不出现 embedding 配置项（R-67）；这也意味着即便访客不配 LLM 凭证，索引构建仍会消耗 owner 的 embedding 额度，故 R-49 的拦截必须发生在提交阶段而非分析中途。
- **002 的视觉成果继续用。** `styles.css` 的色阶令牌体系是 002 的产物且已落地，本次沿用（R-61），不重新定义配色。

## Planning Recheck

| item | why recheck | required before | blocks planning? |
| --- | --- | --- | --- |
| IP 限流阈值（示例值：每小时 3 次） | owner 选定的是「做限流」这一形态，具体数字来自选项预览示例；公网实际流量下需调 | 限流层实现 | no |
| 并发上限（示例值：2 个任务） | 与 `llm_max_concurrency=2` 的关系需理清——那是 LLM 在途请求数，不是分析任务数，两者是独立旋钮 | 并发闸门实现 | no |
| 磁盘配额容量 | 未与 owner 确认具体 GB；需按部署机器实际磁盘定 | 配额实现 | no |
| 语言分布展示项数 | `LanguageProfile.by_language` 是全量映射，界面需截断，N 未定 | 概览条实现 | no |
| 节点图布局算法 | 力导向 / 分层 / 手写坐标各有取舍；模块数通常 < 40，实现时选 | Architecture 页实现 | no |
| PDF 生成依赖选型 | weasyprint / reportlab / 前端打印，中文字体配置各不同 | 导出实现 | no |
| 严重度取值兜底 | 现只映射 high/medium/low，后端若返回其它值需兜底段（继承 002 的同名项） | 评审页实现 | no |
| 测试改写范围核对 | 估 15~20 个测试需改写，实际数量在实现时确认；NA-08 要求改写后不少于 53 个 | 前端改造 | no |
| 落盘文件的清理策略与历史链接失效 | 磁盘配额 LRU 清理会让旧 URL 失效（R-05 已覆盖呈现），但清理顺序是否需保留最近 N 条未定 | 持久化实现 | no |

## Outstanding Questions

| id | question | PRD write target | owner_status | blocks_planning | closure_disposition | planning_would_invent_what | closure_state | recommended_default |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| OQ-1 | 公网部署后 LLM 凭证与额度归谁、由什么保护 | R-47~R-52、BR-004、Source-Of-Truth Resolution、NA-02/NA-03 | answered | no | owner-answered | no | closed | 访客自带 key，服务端不存凭证（owner 已选定，见 Owner Decision Trace 第 1 行 / OQ-1） |
| OQ-2 | 架构评分、代码质量、安全分、技术债、星级的口径怎么定 | R-06~R-08、R-35、R-42、NA-01 | answered | no | owner-answered | no | closed | 不造评分，全部换成可派生真实计数（owner 已选定，见 Owner Decision Trace 第 2 行 / OQ-2） |
| OQ-3 | 9 个导航页与「三个 h2 共存」的 40 个测试如何取舍 | R-01~R-02、BR-001、NA-08 | answered | no | owner-answered | no | closed | 改真多页切换并改写测试（owner 已选定，见 Owner Decision Trace 第 3 行 / OQ-3） |
| OQ-4 | 架构图、代码查看器/搜索、导出、持久化四项哪些进本期 | Change Delta extend 行、Scope Boundaries 本期做、R-09~R-45 | answered | no | owner-answered | no | closed | 四项全进（owner 多选，见 Owner Decision Trace 第 4 行 / OQ-4） |
| OQ-5 | Performance、Bug Review、MCP 状态灯、调用链四项无数据源如何处理 | R-14、R-59、Change Delta remove 行、Scope Boundaries Non-Goals、NA-05/NA-06 | answered | no | owner-answered | no | closed | 按真实能力重构导航，去掉这四项（owner 已选定，见 Owner Decision Trace 第 5 行 / OQ-5） |
| OQ-6 | 既有 002 PRD（ready-for-planning，四条硬约束被推翻）如何处置 | 002 frontmatter、Change Delta 历史逻辑说明、Scope Boundaries 关系段 | answered | no | owner-answered | no | closed | 002 标 superseded 并指向 003，新建本文件（owner 已选定，见 Owner Decision Trace 第 6 行 / OQ-6） |
| OQ-7 | 访客自带 key 后仓库克隆仍任意可触发，本期怎么护服务器资源 | R-53~R-57、AE-15 | answered | no | owner-answered | no | closed | IP 限流 + 并发闸门 + 磁盘配额（owner 已选定，见 Owner Decision Trace 第 7 行 / OQ-7） |
| OQ-8 | 最近分析持久化存哪里 | R-43~R-46、AE-12、Source-Of-Truth Resolution | answered | no | owner-answered | no | closed | JSON 文件落盘（owner 已选定，见 Owner Decision Trace 第 8 行 / OQ-8） |
| OQ-9 | 代码查看器右栏的「AI 分析」来源 | R-19~R-21、NA-07、AE-05 | answered | no | owner-answered | no | closed | 只复用已有评审发现，零新增 LLM 调用（owner 已选定，见 Owner Decision Trace 第 9 行 / OQ-9） |
| OQ-10 | 嵌入模型凭证是否也开放给访客配置 | R-67、Source-Of-Truth Resolution、Evidence And Assumptions | answered | no | owner-answered | no | closed | 不开放，用 owner 自己的 API（owner 已确认，见 Owner Decision Trace 第 11 行 / OQ-10） |
| OQ-11 | 限流阈值、并发上限、磁盘配额、节点图布局算法、PDF 依赖选型的具体取值 | Planning Recheck | not-asked | no | implementation-only-how-pushdown | no | closed | 由实现阶段在 R-53~R-55 与 R-09 的产品约束内决定。不触及接口可用性、权限、范围、source-of-truth、兜底展示或统计验收——限流「要有且可解释」已由 R-53 锁定，具体数字不改变产品行为语义 |
| OQ-15 | 页面底色用配色原三色还是压暗变体 | R-62、R-63、Evidence And Assumptions | answered | no | owner-answered | no | closed | owner 附配色图要求以原三色为底色并让控件跟着主色调走。实现期曾自行压暗（理由是全饱和的粉会让面板像贴纸），但那未满足 R-62 的字面要求。改回后 R-63 由分层兜住：内容列 88% 不透明托底 + 面板从基调提亮，实测文字落在 9.8–13.5:1 的面上（渐变三色自身为 8.88 / 3.52 / 1.05，仅深蓝端可直接承载文字） |
| OQ-14 | 准入门限的具体数值定在哪一档 | Current System Snapshot 准入门限行、R-56 | answered | no | owner-answered | no | closed | owner 要求放宽：「可解析文件数和仓库体积限制太小了，很多好的项目都超过设定范围了」。定为 8000 个可解析文件 / 1.5GB（原 1500 / 300MB）。取 8000 是按 TS 项目那一档——实测 refine 6810，定 6000 会把它挡回去。**R-56 的语义不受影响**：它约束的是「沿用现有拒绝逻辑并说明原因」，而拒绝逻辑、reason 与文案均未改，超限仍拒（`tests/test_ingest_admission.py::test_parseable_gate_still_rejects_above_limit`）。AE 中引用的是符号名 `max_parseable_files` 而非数字，故验收表述不变 |
| OQ-13 | R-03 的「标注原因」以什么形态呈现 | R-03、AE-01、Evidence And Assumptions | answered | no | owner-answered | no | closed | owner 本轮实机核对后要求去掉可见文案：「只是没有输入仓库链接分析的时候点击不了即可，不需要文字描述」。原因改由 `title` 与 `aria-label` 承载。理由是九项导航里有七项会同时置灰，每项挂一句相同说明即七行重复噪声。**已知代价**：disabled 按钮不可聚焦，故视力正常的键盘用户取不到该原因（鼠标悬停与读屏仍可读）。AE-01 的「每项标注」字面仍成立，R-03 价值里的「原因可读」在该人群上变弱 |
| OQ-16 | 网络类失败是否退还限流配额、是否设退还次数上限 | R-69、R-70、R-53 修订说明、AE-24、NA-11、NA-12、Exception Handling、Evidence And Assumptions | answered | no | owner-answered | no | closed | owner 选定「保留无上限退还，补写进 PRD」（见 Owner Decision Trace 的 OQ-16 行）。理由是按 3 次/小时的默认值，三次网络抖动就把人锁一小时而他什么都没做错；这与「未配置凭证不计数」同源——没有产生有效分析的提交不扣配额。**已知代价**：反复触发网络失败这一路径不受限流约束，量化取舍见 Evidence And Assumptions 对应行 |
| OQ-12 | R-53 的按 IP 计数依赖什么部署前提 | R-53、Evidence And Assumptions 末行、README 公网部署一节 | answered | no | owner-answered | no | closed | 「反代必须透传真实客户端 IP，否则限流按反代 IP 计数、全站共用一个配额（这是 R-53 的前提）」——owner 已在 plan 的 KTD9 定选（`session-settled: user-directed`，见 `docs/plans/2026-08-26-001-feat-code-intelligence-workbench-plan.md:815`，与 Owner Decision Trace 第 7 行 / OQ-7 的「做限流」形态一致）。实现后容器实跑量化了该前提的后果，见 Evidence And Assumptions 末行 |

## Owner Decision Trace

| question | owner_answer/source | chosen_answer | PRD write target | consequence | closure_state |
| --- | --- | --- | --- | --- | --- |
| OQ-1：设置页能改 LLM URL/KEY，而后端目前没有鉴权层。上线到公网后，凭证和额度归谁、由什么保护 | 选定「公网 + 访客自带 key，服务端不存凭证」，选项预览含「LLM key 仅存访客 localStorage / 服务端不存凭证 / 仓库克隆仍需限流」 | 访客在设置页填自己的 base_url 与 key，只存浏览器；服务端不落盘不记日志；未配置时不回落服务端凭证；不做登录 | R-47~R-52、BR-004、NA-02/NA-03、Source-Of-Truth Resolution | 方案里的「用户登录」不做；服务器资源风险转由 OQ-7 处理 | closed |
| OQ-2：方案要显示架构评分 82、代码质量 75、安全 65、技术债 25h、Agent 星级。后端没有任何评分机制，口径怎么定 | 选定「换成可派生的真实计数」，预览含「文件数 33 / 模块 9 / 发现 6 / 切块 99」与「评审执行情况表 + 引用校验 25/26 + 未解析 7 个文件」 | 删除全部评分、星级、技术债、Stars、代码行数；改为可指回后端字段的真实计数 | R-06~R-08、R-35、R-42、NA-01 | 每个界面数字必须能指回字段来源；报告页评分条改为评审执行情况表 | closed |
| OQ-3：方案要 9 个导航页，但现有 40 个测试依赖「完成态下报告/评审/问答三个 h2 同时在 DOM」（002 的 BR-003） | 选定「改为真多页切换，改写相关测试」，预览含「任一时刻只有一个 h2 在 DOM / 估 15~20 个测试要改」 | 废除 002 的 BR-003，改客户端路由多页；改写测试写法但不删断言 | R-01~R-02、BR-001、NA-08 | 002 的 BR-003 失效；测试改写后不得少于 53 个 | closed |
| OQ-4：架构图可视化、代码查看器+搜索、报告导出、最近分析持久化，哪些进本期 | 四项全选 | 四项全部进本期 | Change Delta extend 行、Scope Boundaries 本期做、R-09~R-45 | 推翻 002 的 BR-002（后端零改动）与 BR-006（不引入图表库），以及 plan 的「浏览式代码浏览器在产品定位之外」；新增 5 类后端端点 | closed |
| OQ-5：导航里 Performance、Bug Review、MCP 状态灯三项后端没有对应数据，调用链也取不到，怎么处理 | 选定「按真实能力重构导航」，预览含「Bug Review 改名 Error Handling / 去掉 Performance / Architecture 只留模块图无调用链 / MCP 只做配置说明页」 | 去掉 Performance 页与调用链子项；Bug Review 改名 Error Handling；新增 Structural 页；MCP 页只做配置说明 | R-14、R-59、Change Delta remove 行、Non-Goals、NA-05/NA-06 | 导航仍 9 项但组成按真实能力重排；四项无数据源的展示全部不做 | closed |
| OQ-6：已有一份同主题 PRD（2026-08-25-002，ready-for-planning），但它的 BR-001/002/003/006 本次全部被推翻，怎么处置 | 选定「旧 PRD 标为 superseded，新建一份」，预览含「002 status: superseded + superseded_by: 003 / 两份都在历史可追 / plan 只认 003」 | 002 改 status: superseded 并加 superseded_by 指向 003；本文件为新 spec_id | 002 frontmatter、Change Delta 历史逻辑说明、Scope Boundaries 关系段 | 002 的 owner 决定轨迹（D1~D4）保留可查；planning 只认 003 | closed |
| OQ-7：访客自带 key 后，仓库克隆仍是任意访客可触发（克隆磁盘 + 向量化 + LLM 调用），本期怎么护住服务器资源 | 选定「IP 限流 + 并发闸门」，预览含「第 4 次提交返回 429 与可重试时间 / 第 3 个任务排队并显示位置 / 磁盘配额克隆总量上限」 | 新增单 IP 时间窗限流、全局并发任务上限（超出排队）、仓库副本磁盘配额（LRU 清理，不清正在分析的） | R-53~R-57、AE-15、Exception Handling | 公网部署有资源保护；配额清理会让旧历史链接失效，由 R-05 覆盖呈现 | closed |
| OQ-8：最近分析持久化存哪里（现在任务状态在进程内存，重启即丢） | 选定「JSON 文件落盘」，预览含「.workspace/analyses/{task_id}.json / 启动时扫目录 / 没有 DB 没有 migration / 重启后历史可读但任务仍不可恢复」 | 分析完成时把 AnalysisResult 写为 JSON；启动扫目录重建列表；单文件损坏跳过并计数 | R-43~R-46、AE-12、Source-Of-Truth Resolution | 不引入数据库依赖；未完成任务仍不可恢复且界面不得误导 | closed |
| OQ-9：代码查看器右栏的「AI 分析」从哪里来 | 选定「只复用已有评审发现」，预览含「显示 path+line+message+evidence / 没有发现的文件显示『本文件无评审发现』/ 零新增 LLM 调用，零幻觉风险」 | 右栏只列该文件已有的评审发现；打开文件不触发任何 LLM 调用 | R-19~R-21、NA-07、AE-05 | 没有发现的文件右栏是空态（区分「不在评审范围」与「已评审零命中」） | closed |
| 渐变配色的用法（002 曾限制渐变只用三处） | 用户原话：「背景底色按我给你的渐变色为底色」 | 渐变作为页面底色；002 的 R-07「渐变限用三处」被推翻 | R-62、R-63、BR-005、Design Source Coverage 冲突项 | 按 owner-answer fidelity 原样落为 R-62，不软化为「限用三处」；可读性下限单独由 R-63 兜住 | closed |
| OQ-10：嵌入模型凭证是否也开放给访客配置 | 用户原话：「嵌入凭证用我的 api，不开放」 | 嵌入侧用 owner 自己的 API，不开放给访客；设置页只暴露 LLM 凭证 | R-67、R-68、Source-Of-Truth Resolution、Evidence And Assumptions、Decision Notes D10 | 向量化成本由 owner 承担；索引分支（`builder.py:166`）不依赖 LLM 凭证，故未配置凭证的拦截必须在提交阶段完成，否则仍会烧掉 owner 的 embedding 额度 | closed |
| OQ-15：页面底色用配色原三色还是压暗后的变体 | 用户原话（2026-08-26 实机核对，附配色图）：「你的空间和背景色都要改，基于这个颜色来，底色为这个，然后其他的按钮和控件基于这个主色调微调」 | 用配色原三色 `#303d67 → #79799a → #fddfdc`，不压暗；控件按该主色调统一 | R-62、R-63、Evidence And Assumptions | 实现期我曾自行改用压暗变体，理由是「全饱和的粉会让面板像贴纸」—— 但那未满足 R-62 的字面要求（「以**该**三色渐变作为底色」）。改回原三色后 R-63 的兜底方式变为分层：内容列 88% 不透明托底 + 面板色系从基调提亮，实测文字始终落在 9.8–13.5:1 的面上。控件侧同步：主按钮用柔粉配深字（柔粉唯一对比度足够的用法），次级按钮用面板提亮色加强描边，输入框比面板再深一档并用柔粉低透明光晕做焦点态 | closed |
| OQ-14：准入门限的具体数值定在哪一档 | 用户原话（2026-08-26）：「可解析文件数和仓库体积限制太小了，很多好的项目都超过设定范围了，请放宽」 | `max_parseable_files` 1500 → 8000；`max_repo_size_kb` 300000 → 1500000（1.5GB） | Current System Snapshot 准入门限行、R-56、Evidence And Assumptions | 放宽的成本只落在 embedding（唯一按量付费且线性增长的环节）：实测 fastapi 1138 文件 → 719 块 → 索引 278s，外推 8000 文件约 5000 块、约 32 分钟；索引按 repo+commit+provider 缓存，每 commit 只付一次。连带两处必须同调：`max_graph_nodes` 2000 → 8000（依赖图是模块聚类的输入，否则介于两值之间的仓库会拿到目录级架构视图），clone 超时 180s → 600s（否则大仓库以 NETWORK_ERROR 失败而非在准入处干净拒绝）。真正的天花板是「提交后等结果」这个交互形态——单次超过半小时需要断点续跑，属独立工作 | closed |
| OQ-13：置灰导航项的「标注原因」是否必须是可见文案 | 用户原话（2026-08-26 实机核对）：「太多的这种 AI 制作的痕迹了，去掉文字，只是没有输入仓库链接分析的时候点击不了即可，不需要文字描述」 | 去掉可见文案；原因改由 `title` 与 `aria-label` 承载 | R-03、AE-01、Evidence And Assumptions | 版面从七行重复说明降到零行。代价是 disabled 按钮不可聚焦，视力正常的键盘用户取不到原因——鼠标悬停与读屏不受影响。R-03 的措辞不改：AE-01 的「每项标注」仍成立，变的是呈现形态 | closed |
| OQ-16：网络类失败退还限流配额的口径（退还本身与 D6「保护克隆磁盘与带宽」的理由存在张力，且每次这样的失败仍消耗一次克隆尝试与三次重试退避） | 用户选定「保留无上限退还，补写进 PRD」（2026-08-26，实现后回填；选项预览含 `R-69` 条文、三处 source 引用与「退还次数无上限」的已知取舍） | 保留现有实现：瞬时网络失败退还该 IP 一格配额，退还次数不设上限；R-53 的计数语义随之读作「非网络失败的提交」 | R-69、R-70、R-53 修订说明、AE-24、NA-11、NA-12、Exception Handling 三行、Evidence And Assumptions | 用户可在网络抖动后立即重试而不被锁一小时；代价是「反复触发网络失败」这一路径不受限流约束，见 Assumptions 中的量化取舍行。本行为实现先行、PRD 后补的回填决定：代码与测试在 2026-08-26 会话中先落地，本轮补齐产品语义 | closed |
| OQ-12：R-53 的按 IP 计数依赖什么部署前提 | plan KTD9（`session-settled: user-directed`）：「反代必须透传真实客户端 IP，否则限流按反代 IP 计数、全站共用一个配额（这是 R-53 的前提）」，见 `docs/plans/2026-08-26-001-feat-code-intelligence-workbench-plan.md:815` | 接受「反代覆写 `X-Forwarded-For`」为公网部署的必要前提；不在应用层检测或拒绝直接暴露 | R-53、Evidence And Assumptions 末行、README 公网部署一节 | 直接暴露容器端口时按 IP 限流不生效——实测（2026-08-26 容器实跑）外部客户端在后端被统一记录为 Docker 网桥网关 `172.20.0.1`，「每 IP 每小时 3 次」塌为全站一个配额。限流机制本身正确，失守的是它的输入。据此 README 把反代从「加固项」改述为「必要条件」，nginx 示例改用 `$remote_addr` 覆写而非 `$proxy_add_x_forwarded_for` 追加 | closed |

## 需求追溯矩阵

| 需求编号 | 关联业务规则 | 验收编号 | 约束 / 风险 | 证据 / 规则依据 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| R-01 | BR-001 | AE-01 | 多页外壳重构影响现有 DOM 与测试 | D5 线框 | P0 |
| R-02 | BR-001 | AE-01 | 路由引入需新依赖或自实现 | D3 | P0 |
| R-03 | — | AE-01 | 未就绪页可点会导致空页 | D5 | P0 |
| R-04 | — | AE-17 | 窄视口横向溢出 | plan U13 响应式 | P1 |
| R-05 | — | AE-18 | 配额清理后旧链接必然失效 | D7 | P0 |
| R-06 | BR-002 | AE-02 | — | D2 | P0 |
| R-07 | BR-002 | AE-02、NA-01 | 编造数字最易被追问 | D2 | P0 |
| R-08 | BR-002 | AE-02、NA-01 | 前端合成数字无法核验 | D2 | P0 |
| R-09 | BR-006 | AE-03 | 需引入图形渲染，破 002 的 BR-006 | `static_analysis/models.py:145-154` | P0 |
| R-10 | — | AE-03 | — | `Module` + `ModuleAnalysis` 字段 | P0 |
| R-11 | — | AE-03 | 二次概括会脱离引用校验 | `graph/state.py:36-47` | P0 |
| R-12 | — | AE-03 | — | D4 | P1 |
| R-13 | — | AE-04 | 静默粗粒度会误导拓扑理解 | `DependencyGraph.granularity` | P0 |
| R-14 | — | AE-04、NA-05 | 无数据源 | `README.md:130-132` | P0 |
| R-15 | — | AE-05 | 破 plan「代码浏览器在定位之外」 | D4 线框 | P0 |
| R-16 | — | AE-05 | 需新增后端端点 | 现状无此端点 | P0 |
| R-17 | BR-007 | AE-06、NA-04 | 任意文件读取是严重漏洞 | `backend/paths.py` KTD14/15 | P0 |
| R-18 | — | AE-06 | 静默截断会误导 | `max_file_bytes` | P0 |
| R-19 | — | AE-05 | — | `review/models.py` Finding | P0 |
| R-20 | — | AE-05 | 三种空态混同会误导覆盖范围 | plan R17 | P0 |
| R-21 | — | AE-05、NA-07 | 实时调 LLM 引入成本与幻觉面 | D9 | P0 |
| R-22 | — | AE-07 | line=0 跳转会失败 | `review/models.py` 注释 | P1 |
| R-23 | — | AE-07 | — | D4 | P0 |
| R-24 | — | AE-08 | 需新增后端端点 | `mcp_server/server.py:262-302` | P0 |
| R-25 | — | AE-08 | 两者混同会让用户误判下一步 | `mcp_server/server.py:285-292` | P0 |
| R-26 | — | AE-08 | — | D4 | P1 |
| R-27 | — | — | 现有能力不得退化 | `rag/qa.py:83-95` | P0 |
| R-28 | — | AE-07 | — | D4 | P1 |
| R-29 | — | — | 单轮被误解为多轮 | `README.md:138` | P1 |
| R-30 | — | AE-09 | — | `FindingCategory` 三类 | P0 |
| R-31 | — | AE-09、NA-09 | 未执行显示为零发现是错误陈述 | plan R17 | P0 |
| R-32 | — | AE-09、NA-09 | — | `CategoryOutcome.scope` | P0 |
| R-33 | — | AE-07 | — | D4 | P1 |
| R-34 | — | AE-10 | — | `ReportModel` 字段 | P0 |
| R-35 | BR-002 | AE-10、NA-01 | — | D2 | P0 |
| R-36 | — | AE-10 | 隐藏不可核验结论会夸大报告可信度 | `ReportModel.unsupported_claims` | P0 |
| R-37 | — | AE-10 | 截断破坏可核验性 | 继承 002 R-09 | P0 |
| R-38 | — | AE-14 | PDF 需新依赖 | D4 | P1 |
| R-39 | BR-003 | AE-14 | 导出与界面口径不一致会自相矛盾 | BR-003 | P0 |
| R-40 | — | AE-14 | 损坏文件比失败更糟 | 设计判断 | P1 |
| R-41 | — | AE-11 | — | D5 线框 | P0 |
| R-42 | BR-002 | AE-11、NA-01 | — | D2、D7 | P0 |
| R-43 | — | AE-12 | — | D7 | P1 |
| R-44 | — | AE-12 | 单文件损坏不得拖垮列表 | D7 | P1 |
| R-45 | — | AE-11 | — | D7 | P1 |
| R-46 | — | AE-11、NA-10 | 声称可恢复是错误陈述 | `README.md:141-143` | P0 |
| R-47 | BR-004 | AE-13 | — | D1 | P0 |
| R-48 | BR-004 | AE-13、NA-02 | 凭证落盘或进日志不可接受 | D1 | P0 |
| R-49 | BR-004 | AE-13、NA-03 | 回落会消耗他人额度 | D1 | P0 |
| R-50 | — | AE-13 | 归因不清会让访客以为是服务问题 | 设计判断 | P0 |
| R-51 | BR-004 | AE-13、NA-02 | 回显完整 key 等于泄露 | D1 | P0 |
| R-52 | — | — | — | D1 | P1 |
| R-67 | BR-004 | AE-23 | 暴露 embedding 配置会让索引键分裂、缓存全失效 | D10、`backend/cache/key.py` | P0 |
| R-68 | BR-004 | AE-23 | 拦截过晚会消耗 owner 的 embedding 额度 | D10、`backend/graph/builder.py:166` | P0 |
| R-53 | — | AE-15 | 无限流则公网下资源可被耗尽 | D6 | P0 |
| R-54 | — | AE-15 | 直接拒绝比排队体验更差 | D6 | P0 |
| R-55 | — | AE-15 | 清理正在分析的仓库会导致分析失败 | D6 | P0 |
| R-69 | — | AE-24、NA-11 | 退还无上限，网络类失败可反复占用克隆尝试 | D11、`api/ratelimit.py:112` | P0 |
| R-70 | — | AE-24、NA-12 | 重试放大单次提交的服务器开销 | D11、`ingest/clone.py:117-142` | P0 |
| R-56 | — | — | 现有能力 | `ingest/admission.py` | P0 |
| R-57 | BR-007 | AE-06、NA-04 | 暴露服务器其它路径是严重漏洞 | KTD14 | P0 |
| R-58 | — | AE-16 | — | `mcp_server/server.py:82-262` | P1 |
| R-59 | — | AE-16、NA-06 | 不可观测的状态灯是假信息 | `README.md:95-96` | P1 |
| R-60 | — | AE-16 | 缺前置条件说明会让接入失败 | `README.md:112` | P1 |
| R-61 | — | AE-17 | — | `styles.css:12-37` | P0 |
| R-62 | BR-005 | AE-17 | 大面积渐变可能压过可读性 | D8（owner 明确要求） | P1 |
| R-63 | BR-005 | AE-17 | 可读性优先于观感 | 设计判断 | P0 |
| R-64 | — | AE-17 | 键盘不可达 | plan U13 可访问交互 | P0 |
| R-65 | — | AE-17 | 页面切换后焦点丢失 | plan U13 可访问交互 | P0 |
| R-66 | BR-006 | AE-04 | 图形是唯一通道时不可读 | 可访问性判断 | P0 |

## Readiness Self-Check

write_mode: final-prd
clarification_evidence: asked-owner
preflight_sweep_closure: closed
decision_card_highest_risk_gap: 上线公网 + 设置页可写 LLM 凭证 + 后端无鉴权层三者叠加，等于任意访客可读写服务端凭证并消耗额度；且方案约 60% 的展示字段（评分、技术债、Stars、调用链、Performance 类别、MCP 状态）后端无数据源
decision_card_next_action: final-prd
decision_card_why_no_invention: 部署形态与凭证归属（D1）、评分口径（D2）、导航形态与测试取舍（D3）、四项新能力范围（D4）、无数据源项的处置（D5）、资源保护形态（D6）、持久化形态（D7）、渐变用法（D8）、查看器右栏来源（D9）、嵌入凭证归属（D10）、网络类失败的重试与配额退还（D11）十一项 owner 决定全部闭合；页面结构由 owner 提供的 ASCII 线框锁定；线框与后端能力的六处冲突逐项经 owner 裁定并写入 Negative Acceptance；现状边界（无鉴权、无评分、无调用链、无 performance 类、MCP 不可观测、任务状态易失）全部有 source 证据。实现期实机核对又追加 OQ-12~OQ-16 五项裁定（反代前提量化、准入门限放宽、置灰原因形态、底色用原三色、网络失败退还配额），均已落为 R/AE/NA 或修订既有条目。剩余仅限流阈值、节点图布局算法、PDF 依赖选型等 HOW 细节，planning 不需发明任何产品行为
design_source_coverage: read
first_unclosed_owner_question: none
recommended default: none
can_enter_spec_plan: yes
why_not: none

## 变更记录

| 日期 | 修改人 | 变更内容 |
| --- | --- | --- |
| 2026-08-25 | zzhelp（经 spec-prd） | 初稿。supersede 002。含 D1~D9 九项 owner 决定；线框与后端能力六处冲突的裁定；公网部署形态与凭证边界；四项新增能力范围。 |
| 2026-08-26 | zzhelp（经 spec-prd refine） | 实现期实机核对追加 OQ-12~OQ-15 四项 owner 裁定（反代前提量化、准入门限放宽、置灰原因形态、底色用原三色）。 |
| 2026-08-26 | zzhelp（经 spec-prd refine） | 补记 D11（OQ-16）：网络类失败的自动重试与限流配额退还。新增 R-69、R-70、AE-24、NA-11、NA-12，修订 R-53 的计数语义为「非网络失败的提交」，Exception Handling 补两行网络类场景。此前实现已落地而 PRD 无对应产品语义，属实现先于需求的补写。 |

## Handoff

本 PRD 的 WHAT 已闭合，可进入 `spec-plan` 决定实现顺序与文件级改动。实施时必须带上以下硬约束：

- **BR-002 + NA-01**：界面任何位置不得出现评分、星级、技术债、Stars、代码行数。这是本期与原始方案最大的差异，也是最容易在实现时「顺手加回去」的地方。
- **BR-004 + NA-02/NA-03**：访客凭证不落盘、不进日志、未配置不回落。公网部署的前提。
- **BR-007 + NA-04**：新增文件读取端点必须复用 `resolve_within`，不放宽路径校验。
- **BR-001 + NA-08**：改写测试不得降低覆盖，改写后不少于 53 个。
- **R-21 + NA-07**：打开文件不得触发 LLM 调用。
- **R-20/R-31/R-32 + NA-09**：零命中与未执行必须可区分——这是 plan R17 的已签需求，三个评审页与查看器右栏都要守住。
- **R-69/R-70 + NA-11/NA-12**：退还限流配额只对我们这侧的网络类失败生效，业务类失败（仓库不存在、无权限、超规模）不得退还；「已自动重试且不占配额」这句承诺只能用于后端克隆失败，浏览器连不上后端时必须走另一条提示，否则把用户指向错误的修法。

实现顺序建议（由 `spec-plan` 最终决定）：路由外壳与测试改写 → 后端新增端点（文件读取优先，它是查看器与引用下钻的前提）→ 设置页与凭证透传 → 限流与配额（上公网前必须就绪）→ 节点图 → 导出 → 持久化。
