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
              ┌─────────────────────┴─────────────────────┐
              ↓                                           ↓
     Planner 定深挖范围                            Reviewer 三类检查
              ↓                                           ↓
   ┌──────────┴──────────┐                             评审发现
   ↓                     ↓
模块子 Agent 并行分析   切块与向量索引  ← 与扇出、评审同时进行
   ↓                     ↓
汇总 + 引用校验      单轮代码问答 · 语义检索
   ↓
架构报告
```

图里画在同一层的确实同时执行。索引与模块扇出并行是有意排布的结果，不是自然发生的 ——
LangGraph 按 superstep 推进，同层节点并发而跨层必然串行，所以「哪个节点挂在哪个节点之后」
决定了它与谁并行。索引是最慢的一段（fastapi 实测 30.5 分钟），把它排在扇出之前会让用户
干等半小时才看到第一条模块分析。

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

两个端口默认都只绑 `127.0.0.1`，即不对外可达。对外开放的方式见下面的「公网部署」一节 ——
那需要访客自带 LLM 凭证，并确认三项资源保护的阈值适合你的机器。

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
| `MAX_PARSEABLE_FILES` | `8000` | 准入门限。按 TS 项目那一档定（实测 refine 6810，同规模 Python 项目约其三分之一） |
| `MAX_REPO_SIZE_KB` | `1500000` | 准入副门（1.5GB）。报的是全量 git 对象体积，而实际是浅克隆，所以它拦的是「即使浅克隆也过大」的情形 |
| `MAX_GRAPH_NODES` | `8000` | 依赖图节点上限，与 `MAX_PARSEABLE_FILES` 同值。超出则降级为目录级并在报告标注 |
| `MAX_INDEX_FILES` | `0`（全量） | 送进向量索引的文件数上限。唯一能压低单次分析耗时的旋钮，代价是检索漏内容 |

**放宽门限的代价只落在 embedding**——它是唯一按量付费且随文件数线性增长的环节。实测
fastapi 的 1138 个可解析文件产出 **6529 块、105 批 embedding、30.5 分钟**；线性外推
8000 文件约 46000 块、约 3.5 小时。其余环节不随规模涨：模块分析的扇出恒定为
`MAX_FANOUT_WIDTH`（12 个模块），评审只查挑选出的文件，解析与切块是本地 CPU。

索引与模块扇出、评审并行执行，所以它不阻塞报告链路——提交后约一分钟就能看到模块分析在
推进，整次分析的跨度约等于索引本身的耗时（实测 36.6 → 约 31 分钟）。

索引按 `repo + commit + provider + 索引范围` 缓存，所以这笔时间每个 commit 只付一次，同一
commit 的二次分析秒级命中。压低单次耗时有两个旋钮，作用面不同：`MAX_PARSEABLE_FILES` 只
影响准入（超限的仓库直接被拒），`MAX_INDEX_FILES` 影响索引覆盖范围（按中心度取前 N 个
文件）。后者的代价是未索引的文件在 Code Search 与 AI Chat 里查不到，所以默认全量。

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

### CI

`.github/workflows/ci.yml`，push 到 main 与所有 PR 触发，四个 job 并行。

| job | 跑什么 | 拦的是什么 |
| --- | --- | --- |
| 后端 (ubuntu / windows) | pytest；mypy 只在 ubuntu | 逻辑回归。两个平台都跑的理由见下 |
| 前端 | test / typecheck / build | 组件行为、类型、构建 |
| 从零装依赖 | 不用缓存装一遍再跑测试 | 依赖声明能否解析成一套可用的版本 |
| 镜像构建 | compose config / build，加两条断言 | Dockerfile 与 compose 的回归 |

**后端跑两个平台不是冗余，是路径逃逸覆盖的要求。** 符号链接在 Windows 上要提权（实测
`WinError 1314`），那 2 条测试在 Windows 跳过；junction 是 Windows 专有机制，10 处调用点
在 Linux 跳过。两者都是 reparse point，`realpath` 与 `glob` 都会穿透，任缺一侧都让这道门
只关一半——本地开发在 Windows，所以 Linux 那侧只有 CI 能提供。

**从零装依赖这个 job 刻意不配缓存。** 它要验的就是「依赖能否解析」，缓存复用上次的解析
结果正好绕过要验的东西。`mcp` 的开区间缺陷（`mcp>=1.27` 解析到 2.1.0，而 2.x 移除了
`mcp.server.fastmcp`）就是本机装着旧版所以看不出来，只在全新安装时才暴露。

镜像那个 job 的两条断言值得单独说，它们验的是设计而非「能构建」：

- **默认绑定必须仍是 `127.0.0.1`**。后端没有鉴权层且能克隆任意仓库、消耗 LLM 额度，默认
  对外可达的后果比构建失败严重得多。断言读 `docker compose config` 的 `host_ip`。
- **镜像层内不得有 `.env` 的值**。该 job 会在仓库根造一份只含占位 key 的 `.env`（compose
  用 `env_file` 读它），所以这正是最该查的时机：若日后有人在 Dockerfile 里加 `COPY . .`，
  这条就会红。

两条断言都做过变异验证：`BIND_ADDR=0.0.0.0` 时第一条转红，而第二条的 grep 能命中镜像层内
的已知文本（确认不是假绿灯）。

**独立的检查步骤带 `!cancelled()`，让一轮 CI 给出完整结论。** GitHub Actions 的默认行为是
「前一步成功才跑下一步」，那会让 pytest 红时 mypy 被跳过、后端红时前端被跳过——而它们是
互不依赖的检查，少一半结论就要再推一次才知道。首次运行踩到过这一点。

例外是前端的构建步骤：`npm run build` 是 `tsc -b && vite build`，类型检查红时它必然因同一
原因红，跑它只是把同一条错误报两遍。那里的默认行为恰好是对的。

CI 里不装 `pdf` extra——weasyprint 需要 libpango 与 libharfbuzz，而未安装时导出端点走
`pdf_unavailable` 降级路径（既定行为）。PDF 的中文渲染由容器内实跑核对：缺字体时它照样
生成、字形是方块，单元测试断言不了字形。

## 公网部署

默认形态是本地单用户。对外开放要先理解四件事，它们不是可选步骤。

### 1. 反代终止 TLS，本项目不签发证书

容器只提供 HTTP。证书签发依赖真实域名，与应用层逻辑无耦合，所以不做在这里 —— 用 Caddy、
nginx 或云厂商的负载均衡器终止 TLS，把明文转到 `${BIND_ADDR}:${WEB_PORT}`。

### 2. 反代是按 IP 限流的前提，不是加固项

限流按客户端 IP 计数，IP 取自 `X-Forwarded-For` 的最左项，回落到直连地址。

**直接把容器端口暴露到公网时，按 IP 限流完全不生效。** 这不是理论风险，是实测结论：从局域网
地址（`192.168.31.54`）访问 `BIND_ADDR=0.0.0.0` 起的后端，uvicorn 记录的客户端地址是
`172.20.0.1` —— Docker 网桥的网关。所有外部客户端都是这一个地址，于是三次/小时的配额被全站
共用，表现是「几个人用完之后所有人都被限流」，而服务看起来完全正常。

所以反代不是「更安全的做法」，而是限流成立的**必要条件**：只有它覆写 `X-Forwarded-For` 之后，
不同访客才真正各自计数。

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    # 覆写而非追加：追加会让客户端能自己伪造最左项绕过限流。
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header Host $host;
}
```

链路上有多层代理时才用 `$proxy_add_x_forwarded_for`（它追加而非覆写），并确保最外层那一跳
是你控制的。

### 3. 访客自带 LLM 凭证，服务端不存

公网形态下提交分析必须携带访客自己的 `base_url` 与 API key（在界面的设置页填，存浏览器
localStorage）。服务端不持久化、不记日志、不写入落盘的分析结果。未配置凭证时提交在**克隆
与向量化之前**就被拒绝 —— 否则索引链路会消耗部署者的 embedding 额度。

向量索引仍由服务端统一构建，不暴露 embedding 配置项：索引缓存键含 provider 身份，让访客
各带一份会让键分裂、缓存全部失效。

### 4. 三项资源保护

后端没有鉴权层。护住服务器资源的是三道独立的门，各管一种耗尽方式：

| 保护 | 变量 | 默认 | 超限行为 |
| --- | --- | --- | --- |
| IP 提交限流 | `SUBMISSIONS_PER_WINDOW` / `SUBMISSION_WINDOW_SECONDS` | 3 次 / 3600 秒 | 429 + `Retry-After` |
| 并发闸门 | `MAX_CONCURRENT_ANALYSES` / `MAX_QUEUED_ANALYSES` | 2 / 8 | 排队并给出位置；队列满返回 503 |
| 磁盘配额 | `REPOS_QUOTA_BYTES` / `QUOTA_MIN_RECLAIM_RATIO` | 20GB / 0.10 | 按最近最少使用清理非在跑副本 |

配额清理只删仓库工作副本，**不删**向量索引与落盘的分析历史 —— 被清理的分析仍能从历史列表
载入，只有代码查看器与检索读不到源文件，界面会明说「代码副本已清理」。

`QUOTA_MIN_RECLAIM_RATIO` 是一道下限：可回收量低于配额的这个比例时不清理，本次分析直接
因磁盘不足失败。它防的是「清理与克隆互相追赶」—— 配额贴顶时每次新分析清掉上一个副本，
下次分析同一仓库又要重新克隆，磁盘始终贴顶而克隆成本被反复付出。宁可让「配额太小」这类
配置错误以明确失败暴露。

### 开启与回滚

```bash
# 开启：绑定地址改为对外，并填实际域名
BIND_ADDR=0.0.0.0 CORS_ORIGINS='["https://your.domain"]' docker compose up -d

# 回滚：删掉 BIND_ADDR 并重启
docker compose up -d
```

回滚是一步可逆动作，不涉及数据迁移（本期无 schema 变更），落盘的分析历史在回滚后仍可读。

### 一个容易踩的泄露路径

`docker compose config` 会把 `.env` 里的值**明文展开**到 stdout，包括 `DEEPSEEK_API_KEY` 与
`EMBEDDING_API_KEY`。它是排查配置的常用命令，输出也常被贴进 issue 或聊天里 —— 贴之前先过一遍
脱敏：

```bash
docker compose config | sed -E 's/((KEY|TOKEN)\s*:\s*).*/\1[REDACTED]/'
```

### 出现问题时看哪里

三个失败模式的共同特征是**从外部看起来像正常运行**：限流把真实访客全拒了、队列积压导致
提交后长期无进展、配额贴顶导致反复克隆 —— 三者都不产生错误页，健康检查也全绿。

- `GET /api/health` 给出在途任务数、队列长度、工作副本占用、配额上限与最近一次回收量。
- 每次拒绝写一条结构化日志，形如 `reject reason=rate_limited client=… count=… limit=…`。
  `reason` 是稳定字段，可按它计数；取值为 `credentials_required`、`rate_limited`、
  `queue_full`、`disk_quota` 与既有的准入拒绝原因。

出现滥用（单 IP 绕过限流、磁盘被打满、embedding 额度异常消耗）时，执行上面的回滚。

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

**循环依赖只报导入期成立的环。** 依赖图区分「导入期依赖」与「调用期依赖」：函数体内的
延迟导入与 `if TYPE_CHECKING:` 块内的导入照常算依赖边（中心度、聚类、架构结论都算它们），
但不参与环检测 —— 环上有这样一条边，导入期就不构成环，而那往往正是作者用来打破循环的
手段。这类环只在评审的范围说明里计数，不逐条报出。

**问答是单轮的**，不保留上下文，不做 query 改写。

**没有鉴权层。** 公网形态下护资源的是限流、并发闸门与磁盘配额三项，加上「访客自带 LLM
凭证」使 LLM 成本归属访客自己。这三者护的是服务器资源，不是访问控制 —— 任何人都能提交
分析、读到任何一次分析的结果。要区分用户就得自己加鉴权。MCP server 仍假设单用户本地运行。

**分析任务状态在进程内存里。** 服务重启后未完成的任务丢失；已完成的索引缓存仍在，重新
提交同一仓库会秒级命中。这也是后端固定单 worker 的原因。

## 许可

未指定。
