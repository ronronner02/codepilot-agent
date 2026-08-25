# CodePilot MCP Server

以标准 MCP 协议对外暴露仓库分析工具。任何 MCP 客户端（Claude Code、Cursor 等）都能连接，
无需本项目的界面参与。

## 前提

工具读取的是**已分析仓库**的本地数据。分析由本项目的界面或 API 完成：

```bash
curl -X POST http://localhost:8000/api/analyses \
  -H 'Content-Type: application/json' \
  -d '{"repo_url": "https://github.com/fastapi/fastapi"}'
```

分析产出两份数据，MCP 工具分别依赖它们：

| 数据 | 位置 | 哪些工具需要 |
| --- | --- | --- |
| 克隆的代码 | `.workspace/repos/<owner>__<name>/` | 除 `search_code` 外全部 |
| 向量索引 | `.workspace/index/` | 仅 `search_code` |

所以 embedding provider 变更后 `search_code` 会报索引键不匹配，而其余工具照常可用 ——
它们只需要代码本身。

## 启动

```bash
python -m backend.mcp_server.server
```

stdio 传输，无需端口。日志走 stderr —— stdout 用于协议帧，往那里写日志会破坏协议。

## 客户端配置

### Claude Code

`~/.claude.json` 或项目的 `.mcp.json`：

```json
{
  "mcpServers": {
    "codepilot": {
      "command": "python",
      "args": ["-m", "backend.mcp_server.server"],
      "cwd": "E:/CodePilot-Agent"
    }
  }
}
```

`cwd` 必须是项目根 —— 服务从 `.env` 读配置，并按相对路径定位 `.workspace`。

### Cursor

`~/.cursor/mcp.json`，格式同上。

### 调试

```bash
mcp dev backend/mcp_server/server.py
```

MCP Inspector 会在浏览器打开，可逐个核对工具 schema 与实际调用结果。

## 工具

| 工具 | 用途 | 需要索引 |
| --- | --- | --- |
| `list_repos` | 列出已分析的仓库。**先调这个**拿到可用的 `repo` 标识 | 否 |
| `read_repo_structure` | 列目录的直接子项（只一层） | 否 |
| `read_file` | 读文件的指定行范围 | 否 |
| `find_definition` | 按名字查符号定义，返回路径与行号 | 否 |
| `find_references` | 查符号的引用点 | 否 |
| `query_dependencies` | 文件的双向依赖与中心度；不给 path 时返回整体概览 | 否 |
| `search_code` | 语义检索代码片段 | 是 |

`repo` 参数接受 `owner/name`、工作目录名（`owner__name`）或完整仓库地址 —— 三种写法都行。

## 两处需要知道的边界

**`find_references` 是依赖图定界的文本匹配，不是真正的引用解析。** 静态解析层只提取定义
与 import 声明，不提取调用点。所以它先由符号表定位定义文件，再在导入该文件的文件里做文本
检索 —— 比全仓库 grep 精确得多，但同名局部变量与注释里的同名文本也会命中。结果里会附上
这条说明。

**路径校验不放宽。** 所有读文件的工具都过同一个校验函数：路径必须落在仓库工作目录内，
且路径上任何一段是符号链接或目录联接都会被拒。这与内部管道用的是同一份实现，MCP 边界不
额外放宽也不额外收紧。

## 与内部管道的关系

工具实现全部在 `backend/tools/`，MCP server 是其上的薄胶水，只做协议翻译、仓库标识解析与
错误映射。内部的模块子 Agent 调的是同一批函数 —— 不维护两套逻辑，所以 MCP 客户端看到的
行为与内部分析一致。
