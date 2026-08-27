/**
 * MCP 配置说明页（U18，R-58~R-60、NA-06）。
 *
 * 静态内容页：7 个工具的名称与用途、两个客户端的配置片段、前置条件说明。
 *
 * **不显示运行状态灯**（R-59、NA-06）。MCP 走 stdio 传输，由客户端自己拉起子进程——后端
 * 观测不到它是否在跑。显示状态灯等于显示假信息：绿灯不代表连上了，红灯也不代表没连上。
 *
 * 工具清单与 `backend/mcp_server/server.py` 的定义对应。这里是手写副本而非从后端拉取：
 * 工具集是编译期常量（MCP 不经 HTTP），为它加一个端点只会多一条会漂移的链路。漂移的代价
 * 是「说明页少列一个工具」，而那由本页的测试断言数量兜住。
 */

import { useState } from 'react'
import { usePageHeading } from './usePageHeading'

interface ToolDoc {
  name: string
  purpose: string
}

const TOOLS: readonly ToolDoc[] = [
  { name: 'list_repos', purpose: '列出已分析的仓库。其余工具的 repo 参数要用这里返回的标识。' },
  {
    name: 'read_repo_structure',
    purpose: '列出仓库内某个目录的直接子项（只一层），用于了解组织方式。',
  },
  { name: 'read_file', purpose: '读取仓库内文件的指定行范围，行号 1-based，越界收敛不报错。' },
  {
    name: 'find_definition',
    purpose: '按符号查找定义，返回文件路径与行号。支持裸名、限定名与带路径的写法。',
  },
  {
    name: 'find_references',
    purpose:
      '找符号的引用点。范围由依赖图定界，是文本匹配而非引用解析——同名局部变量也会命中。',
  },
  {
    name: 'query_dependencies',
    purpose: '查询依赖关系：某文件导入谁、被谁导入，以及模块划分与循环依赖。',
  },
  {
    name: 'search_code',
    purpose: '语义检索代码片段，返回带文件路径与行号的结果。需要该仓库已建立向量索引。',
  },
] as const

const CLAUDE_CONFIG = `{
  "mcpServers": {
    "codepilot": {
      "command": "python",
      "args": ["-m", "backend.mcp_server.server"],
      "cwd": "/path/to/CodePilot-Agent"
    }
  }
}`

export function McpPage() {
  const headingRef = usePageHeading<HTMLHeadingElement>()

  return (
    <section className="page">
      <h1 className="page__title" ref={headingRef} tabIndex={-1}>
        MCP
      </h1>

      <p className="page__note">
        编码助手经这 7 个工具读取已分析仓库的符号表、依赖图与向量索引。
        <strong>需要先完成一次分析</strong>。
      </p>

      <h2 className="page__section-title">工具</h2>
      <dl className="mcp__tools">
        {TOOLS.map((tool) => (
          <div className="mcp__tool" key={tool.name}>
            <dt>
              <code>{tool.name}</code>
            </dt>
            <dd>{tool.purpose}</dd>
          </div>
        ))}
      </dl>

      <h2 className="page__section-title">客户端配置</h2>
      <p className="page__note">
        Claude Code 用 <code>~/.claude.json</code> 或项目里的 <code>.mcp.json</code>，
        Cursor 用 <code>~/.cursor/mcp.json</code>，格式相同。<code>cwd</code> 必须是项目根。
      </p>

      <CopyableBlock label="Claude Code / Cursor" content={CLAUDE_CONFIG} />
    </section>
  )
}

/** 可一键复制的配置片段。复制失败时给出说明——剪贴板 API 在非 HTTPS 下不可用。 */
function CopyableBlock({ label, content }: { label: string; content: string }) {
  const [state, setState] = useState('')

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content)
      setState('已复制')
    } catch {
      setState('无法访问剪贴板，请手动选中复制')
    }
  }

  return (
    <div className="mcp__config">
      <div className="mcp__config-head">
        <span className="mcp__config-label">{label}</span>
        <button type="button" onClick={() => void handleCopy()}>
          复制
        </button>
        {state ? (
          <span className="mcp__config-state" role="status">
            {state}
          </span>
        ) : null}
      </div>
      <pre className="mcp__config-body">{content}</pre>
    </div>
  )
}
