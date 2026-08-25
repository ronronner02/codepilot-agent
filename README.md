# CodePilot-Agent

输入 GitHub 仓库地址，产出三样东西：**可追溯到具体文件的架构分析报告**、针对该仓库的
**单轮代码问答**、以及结构/错误处理/安全三类检查的**代码评审**。仓库分析能力同时以
**MCP server** 形式对外暴露，可被 Claude Code、Cursor 等客户端直接调用。

## 它与同类工具的区别

报告的每条结论必须能落到文件路径，且这一点由程序校验而非提示词约束 —— 引用的路径必须
真实存在、行号必须在文件实际行数内，过不了校验的结论会被丢弃并计入报告的缺失说明。所以
不会出现「本项目采用分层架构、代码结构清晰」这类放到任何仓库都成立的句子。

## 架构

```
仓库地址 → clone 与语言识别 → 静态解析层（tree-sitter）
                                    ↓
                        结构骨架：符号表 · 依赖图 · 模块聚类 · 入口点
                                    ↓
              ┌─────────────────────┼─────────────────────┐
              ↓                     ↓                     ↓
     Planner 定深挖范围        Reviewer 三类检查      切块与向量索引
              ↓                     ↓                     ↓
   模块子 Agent 并行分析         评审发现            单轮代码问答
              ↓
      汇总 + 引用校验 → 架构报告
```

确定的部分用确定手段：文件树、import 依赖图、模块聚类、入口点、技术栈识别全部由静态解析
产出，不过 LLM。需要判断的部分才上 Agent —— Planner 决定深挖哪些模块，模块子 Agent 带工具
读代码，Reviewer 的候选点由 AST 定位后交模型取舍。

RAG 只服务问答链路。架构问题（分几层、谁依赖谁、入口在哪）是图问题，靠 embedding 相似度
问不出答案。

## 快速开始

### 用 Docker（推荐）

```bash
cp .env.example .env
# 编辑 .env，至少填 DEEPSEEK_API_KEY
docker compose up --build
```

打开 <http://localhost>。

两个端口都只绑 `127.0.0.1` —— 后端没有鉴权层（单用户本地运行的有意决定），而它能克隆任意
仓库并消耗 LLM 额度。要在别的机器上访问，得自己加鉴权，不要直接改成 `0.0.0.0`。

索引缓存存在具名卷 `codepilot-data` 里，容器重启后仍在，同一仓库的二次分析会命中缓存并
跳过向量化。

### 本地开发

```bash
# 后端
pip install -e ".[dev]"
python -m uvicorn backend.main:get_app --factory --port 8000

# 前端（另开一个终端）
cd frontend && npm install && npm run dev
```

前端在 <http://localhost:5173>，Vite 的 proxy 会把 `/api` 转到 8000。

## 配置

复制 `.env.example` 为 `.env`。必填只有一项：

| 变量 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | LLM 的 API key。变量名沿用 DeepSeek，但只要端点兼容 OpenAI 协议即可 |

常调的几项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `LLM_MODEL_FLASH` | — | 扇出档（高频低判断）：模块分析 |
| `LLM_MODEL_PRO` | — | 汇总档（低频高判断）：Planner、评审判断、报告汇总 |
| `EMBEDDING_PROVIDER` | `local` | `local` 首次运行会下载模型；`api` 走远端 |
| `LLM_MAX_CONCURRENCY` | `2` | 同时在途的 LLM 请求数。经中转网关时并发过高会触发上游超时 |
| `MAX_PARSEABLE_FILES` | `1500` | 准入门限。TS 项目的文件数天然高于同规模 Python 项目，可能需要调高 |

`.env` 不入版本库，也不入镜像 —— `.dockerignore` 排除了它，compose 用 `env_file` 在运行时
注入。

> **`docker compose config` 会把密钥打印成明文。** compose 在解析配置时会把 `env_file`
> 的值展开，所以那条命令的输出含真实 key —— 排查配置问题时别把它贴进 issue、日志或聊天
> 记录。要看结构又不想暴露值，过滤掉环境变量段再看。

## MCP server

**不在容器里**（它走 stdio 传输，由客户端自己拉起子进程）。配置方式与工具清单见
[`backend/mcp_server/README.md`](backend/mcp_server/README.md)。

最小配置（Claude Code 的 `.mcp.json`）：

```json
{
  "mcpServers": {
    "codepilot": {
      "command": "python",
      "args": ["-m", "backend.mcp_server.server"],
      "cwd": "/绝对路径/CodePilot-Agent"
    }
  }
}
```

工具读取的是**已分析仓库**的本地数据，所以先通过界面或 API 提交一次分析。

## 开发

```bash
python -m pytest              # 后端测试
python -m mypy backend        # 类型检查
cd frontend && npm test       # 前端测试
cd frontend && npm run build  # 前端构建（含类型检查）
```

## 已知边界

这些是有意的取舍，不是待办：

**符号级解析只支持 Python 与 TypeScript。** 其他语言的文件计入文件树与规模统计，但不进
符号表、不建依赖图、不切块。报告的缺失部分会列出它们。

**`find_references` 是依赖图定界的文本匹配，不是引用解析。** 解析层不提取调用点，所以它
先由符号表定位定义文件，再在导入它的文件里做文本检索 —— 比全仓库 grep 精确得多，但同名
局部变量与注释也会命中。工具返回里附了这条说明。

**「死代码」未作为发现产出。** 文件级判据（零入度且非入口）在基准仓库上误报率接近 100%
—— 测试、配置、工具链入口的调用方都在仓库之外。要真正做到需要符号级引用分析。评审的
结构类检查会给出计数，但不逐条列出。

**问答是单轮的**，不保留上下文，不做 query 改写。

**没有鉴权层。** 后端 API 与 MCP server 都假设单用户本地运行。

**分析任务状态在进程内存里。** 服务重启后未完成的任务丢失；已完成的索引缓存仍在，重新
提交同一仓库会秒级命中。这也是后端固定单 worker 的原因。

## 许可

未指定。
