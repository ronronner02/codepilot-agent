/**
 * 工作台外壳：左侧锚点导航 + 顶部仓库标识条 + 主内容区。
 *
 * 导航是**锚点滚动**而非视图切换。这不是风格选择：分析完成后「架构报告」「代码评审」
 * 「代码问答」三个区块必须同时留在 DOM 里（既是 U13 的单页无路由约定，也是现有组件行为
 * 测试的前提——三组测试在同一个完成态下各自等自己的标题）。视图切换会让任一时刻只有
 * 一个区块存在。
 *
 * 当前项由点击直接确定，IntersectionObserver 只在滚动时纠正。反过来做（只靠 observer）
 * 会让点击后的高亮等一帧滚动才生效，而在没有布局的测试环境里永远不生效。
 */

import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'

export interface NavItem {
  id: string
  label: string
  enabled: boolean
}

type Tone = 'idle' | 'busy' | 'ok' | 'bad'

interface Props {
  /** 仓库标识。空串表示尚未提交，顶部条走空态（R-05）。 */
  repo: string
  commitSha: string
  stageLabel: string
  stageTone: Tone
  /**
   * 当前任务标识。放在侧栏底部而非重复顶部条的阶段文案——顶部条是吸顶的，两处显示同一个
   * 状态是纯冗余；而任务标识是查后端结果端点时真正要用的值。
   */
  taskId: string
  navItems: NavItem[]
  children: ReactNode
}

const DOT_TONE: Record<Tone, string> = {
  idle: '',
  busy: ' rail__dot--busy',
  ok: ' rail__dot--ok',
  bad: ' rail__dot--bad',
}

const STAGE_TONE: Record<Tone, string> = {
  idle: '',
  busy: ' topbar__stage--busy',
  ok: ' topbar__stage--ok',
  bad: ' topbar__stage--bad',
}

export function AppShell({
  repo,
  commitSha,
  stageLabel,
  stageTone,
  taskId,
  navItems,
  children,
}: Props) {
  const [active, setActive] = useState(navItems[0]?.id ?? '')

  // 依赖用字符串而非数组：navItems 每次渲染都是新数组，直接依赖会让 effect 每帧重跑。
  const enabledKey = navItems
    .filter((item) => item.enabled)
    .map((item) => item.id)
    .join(',')

  useEffect(() => {
    // 测试环境没有 IntersectionObserver，也没有真实滚动——点击设定的高亮已经够用。
    if (typeof IntersectionObserver === 'undefined') return

    const nodes = enabledKey
      .split(',')
      .filter(Boolean)
      .map((id) => document.getElementById(id))
      .filter((node): node is HTMLElement => node !== null)

    if (nodes.length === 0) return

    const observer = new IntersectionObserver(
      (entries) => {
        const top = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0]
        if (top) setActive(top.target.id)
      },
      // 只认视口上部的区块，否则滚到页尾时最后一个区块永远抢不到高亮。
      { rootMargin: '-12% 0px -68% 0px' },
    )

    nodes.forEach((node) => observer.observe(node))
    return () => observer.disconnect()
  }, [enabledKey])

  const goto = (id: string) => {
    setActive(id)
    const node = document.getElementById(id)
    if (!node) return
    // jsdom 无布局实现，scrollIntoView 可能缺失或抛错；高亮不依赖它成功。
    try {
      node.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
    } catch {
      node.scrollIntoView?.()
    }
  }

  return (
    <div className="shell">
      <aside className="rail">
        <div className="rail__brand">
          <span className="rail__mark" aria-hidden="true" />
          <span>
            <span className="rail__name">CodePilot</span>
            <span className="rail__tag">仓库架构分析</span>
          </span>
        </div>

        <nav className="rail__nav" aria-label="分析结果导航">
          <span className="rail__label">工作区</span>
          {navItems.map((item) => (
            <button
              key={item.id}
              type="button"
              className={item.id === active ? 'rail__item rail__item--on' : 'rail__item'}
              disabled={!item.enabled}
              aria-current={item.id === active ? 'true' : undefined}
              onClick={() => goto(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <div className="rail__foot">
          <span className="rail__phase">
            <span className={`rail__dot${DOT_TONE[stageTone]}`} aria-hidden="true" />
            {taskId ? <span className="rail__task">任务 {taskId}</span> : '尚无任务'}
          </span>
        </div>
      </aside>

      <div className="stage">
        <header className="topbar">
          {repo ? (
            <span className="topbar__repo">{repo}</span>
          ) : (
            <span className="topbar__repo topbar__repo--empty">未选择仓库</span>
          )}
          {commitSha ? (
            <span className="topbar__sha">{commitSha.slice(0, 12)}</span>
          ) : null}
          <span className={`topbar__stage${STAGE_TONE[stageTone]}`}>{stageLabel}</span>
        </header>

        <main className="canvas">
          <div className="column">{children}</div>
        </main>
      </div>
    </div>
  )
}
