/**
 * 文件树（U12，R-15、R-16、R-64）。
 *
 * **按需展开单层，不预取全树。** 递归在大仓库上产出上万条目，而用户点开哪个目录才需要哪
 * 一层。代价是每次展开一次请求，换来的是首屏不卡。
 *
 * 节点可 Tab 到达且焦点态可见（R-64）：用 `<button>` 而非 div + onClick——button 自带键盘
 * 语义与焦点态，手工给 div 补 tabIndex/onKeyDown 只会漏掉某一项。
 */

import { useEffect, useState } from 'react'
import { fetchTree } from '../api/client'
import type { FileTreeEntry } from '../api/types'

interface Props {
  taskId: string
  /** 当前打开的文件路径，用于高亮。 */
  currentPath: string
  onOpen: (path: string) => void
  /** 目录读取失败的呈现交给父层——查看器要按 reason 区分「已清理」与「读不到」。 */
  onError: (reason: string, message: string) => void
}

interface DirState {
  entries: FileTreeEntry[]
  note: string
}

export function FileTree({ taskId, currentPath, onOpen, onError }: Props) {
  const [dirs, setDirs] = useState<Map<string, DirState>>(new Map())
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['.']))
  const [loading, setLoading] = useState<Set<string>>(new Set())

  const loadDir = async (path: string) => {
    if (dirs.has(path) || loading.has(path)) return
    setLoading((prev) => new Set(prev).add(path))
    try {
      const tree = await fetchTree(taskId, path)
      setDirs((prev) => {
        const next = new Map(prev)
        next.set(path, { entries: tree.entries, note: tree.truncated_note })
        return next
      })
    } catch (cause) {
      const reason = (cause as { reason?: string }).reason ?? 'unknown'
      const message = cause instanceof Error ? cause.message : '读取目录失败'
      onError(reason, message)
      // 记一个空目录，避免同一个失败的路径被反复重试。
      setDirs((prev) => {
        const next = new Map(prev)
        next.set(path, { entries: [], note: '' })
        return next
      })
    } finally {
      setLoading((prev) => {
        const next = new Set(prev)
        next.delete(path)
        return next
      })
    }
  }

  useEffect(() => {
    void loadDir('.')
    // 只在任务变化时载入根目录。加 loadDir 进依赖会让它每次渲染都重跑。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId])

  const toggle = (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(path)) {
        next.delete(path)
      } else {
        next.add(path)
        void loadDir(path)
      }
      return next
    })
  }

  const renderDir = (path: string, depth: number) => {
    const state = dirs.get(path)
    if (state === undefined) {
      return loading.has(path) ? <li className="tree__pending">载入中…</li> : null
    }
    if (state.entries.length === 0 && depth === 0) {
      return <li className="tree__empty">无可显示文件</li>
    }
    // 深度上限。真实仓库不会有 30 层嵌套，而没有上限时一个自包含的目录条目就能让这个
    // 递归打爆调用栈（实测：RangeError: Maximum call stack size exceeded）。端点侧已挡住
    // 符号链接逃逸，所以这道防护针对的是「响应形状异常」而非「仓库结构异常」。
    if (depth > 30) {
      return <li className="tree__note">目录层级过深，已停止展开</li>
    }

    return state.entries.map((entry) => {
      // 子条目的路径必须真正延伸父路径，否则展开它就是回到自己。
      const extendsParent = path === '.' || entry.path.startsWith(`${path}/`)
      const isOpen = expanded.has(entry.path) && extendsParent
      return (
        <li key={entry.path} className="tree__item">
          <button
            type="button"
            className={
              entry.path === currentPath ? 'tree__node tree__node--on' : 'tree__node'
            }
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
            aria-expanded={entry.is_dir ? isOpen : undefined}
            aria-current={entry.path === currentPath ? 'true' : undefined}
            onClick={() => (entry.is_dir ? toggle(entry.path) : onOpen(entry.path))}
          >
            <span aria-hidden="true">{entry.is_dir ? (isOpen ? '▾' : '▸') : '·'}</span>
            {entry.path.split('/').pop()}
            {entry.is_dir ? (
              <span className="tree__count">{entry.file_count}</span>
            ) : null}
          </button>
          {entry.is_dir && isOpen ? (
            <ul className="tree__children">{renderDir(entry.path, depth + 1)}</ul>
          ) : null}
        </li>
      )
    })
  }

  const rootState = dirs.get('.')

  return (
    <nav className="tree" aria-label="文件树">
      <ul className="tree__root">{renderDir('.', 0)}</ul>
      {rootState?.note ? <p className="tree__note">{rootState.note}</p> : null}
    </nav>
  )
}
