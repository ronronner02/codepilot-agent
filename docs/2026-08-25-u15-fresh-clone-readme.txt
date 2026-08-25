U15 验证：README 的本地启动步骤经全新环境实际走通
日期：2026-08-25
环境：Windows 11，Python 3.13.5（新建 venv），Node（frontend 全新 npm install）

背景
────
此前这条未做的理由是「我的环境已有依赖，看不出遗漏的前置步骤」。本次把 backend/tests/
pyproject.toml/README.md/.env.example 与 frontend 源码复制到一个空目录，新建 venv，
完全照 README 从头走。

结论：抓到一个真实缺陷。README 的步骤本身没错，但依赖声明有一处开区间，导致
「今天全新安装」与「我本机已装」解析出不同版本，且新装的那个直接坏。

────────────────────────────────────────────────────────────────────
发现 1（缺陷，已修）：mcp 依赖开区间导致全新安装即坏
────────────────────────────────────────────────────────────────────

现象
  全新 venv 执行 `pip install -e ".[dev]"` 后：
    pytest 收集失败 —— ERROR tests/test_mcp_server.py
    ModuleNotFoundError: No module named 'mcp.server.fastmcp'

定位
  pyproject.toml 原声明 `mcp>=1.27`（开区间，无上界）。
    本机环境解析到：mcp 1.27.0
    全新安装解析到：mcp 2.1.0
  mcp 2.x 已移除 `mcp.server.fastmcp`，`mcp.server` 的子模块列表变为：
    __main__, _otel, _streamable_http_modern, apps, auth, caching, connection,
    context, elicitation, extension, lowlevel, mcpserver, models, request_state,
    runner, session, sse, stdio, streamable_http, streamable_http_manager,
    subscriptions, transport_security, validation
  而 backend/mcp_server/server.py:27 是 `from mcp.server.fastmcp import FastMCP`，
  按 1.x 的 FastMCP API 写的。

影响
  今天 clone 本仓库的人，MCP server 与它的 10 个测试直接 import 失败。
  本机看不出来，因为本机装的是当时的 1.27.0。
  这与 pyproject.toml 里 Brotli 那条注释记录的是同一类问题的下一个实例。

修正
  pyproject.toml：`mcp>=1.27` → `mcp>=1.27,<2`，并写明上界理由。
  升 2.x 属于独立的迁移工作（API 面不同），不在本次范围。

修正后复验（同一个全新 venv）
  pip install -e ".[dev]" → mcp 1.29.1
  python -m pytest -q     → 700 passed, 1 skipped

────────────────────────────────────────────────────────────────────
发现 2（非缺陷，记录）：缺 DEEPSEEK_API_KEY 时的报错不友好
────────────────────────────────────────────────────────────────────

  未配置 .env 时 `get_app()` 抛裸 pydantic ValidationError：
    ValidationError: 1 validation error for Settings
    deepseek_api_key  Field required [type=missing, ...]
  README 已写明「至少填 DEEPSEEK_API_KEY」，所以这是预期行为而非缺陷。
  但首次使用者看到的是 pydantic 的内部报错，而不是「请先配置 .env」。
  未修：属于体验项，不影响可运行性，也不在本次清单范围内。

────────────────────────────────────────────────────────────────────
其余步骤逐条走通
────────────────────────────────────────────────────────────────────

后端（README「本地开发」节）
  python -m venv .venv                          → ok
  pip install -e ".[dev]"                       → ok，装齐
    重点核对 Brotli：backlog 记录它此前是手动装的。本次全新安装
    `import brotli` 直接可用，说明 pyproject 的 `Brotli>=1.1.0` 声明生效，
    这条担心可以消掉。
    同时核对：langgraph / chromadb / fastapi / tree_sitter 全部可导入。
  python -m uvicorn backend.main:get_app --factory --port 8123  → 启动成功
  curl /api/health                              → HTTP 200
    {"status":"ok","llm_flash":"deepseek-v4-flash","llm_pro":"deepseek-v4-pro",
     "embedding_provider":"local","llm_configured":true}
    （取值来自 .env.example 的默认值，与主环境的 glm-5.2 / api 不同，符合预期）
  python -m pytest -q                           → 700 passed, 1 skipped

前端（README「本地开发」节）
  npm install                                   → added 172 packages
  npm run build                                 → 构建成功
    dist/assets/index-CqrDso7W.css   15.02 kB │ gzip: 3.55 kB
    dist/assets/index-2xiCjCCc.js   213.00 kB │ gzip: 67.29 kB
    与主环境构建产物 hash 一致
  npm test                                      → 53 passed

────────────────────────────────────────────────────────────────────
未覆盖
────────────────────────────────────────────────────────────────────

  本次是「复制文件到空目录 + 新建 venv」，不是从 GitHub 真的 git clone
  （仓库尚无提交，`git log` 报无提交）。差异只在取源方式，依赖解析与启动
  路径完全一致，而缺陷正是在依赖解析处抓到的。
