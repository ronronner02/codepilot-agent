/**
 * 工作台外壳：左侧导航 + 顶部概览条 + 主内容区（R-01）。
 *
 * **导航是路由切换，不是锚点滚动。** U1 之前它靠 `scrollIntoView` 加 IntersectionObserver
 * 纠正高亮，因为那时五个区块必须同时留在 DOM 里。多页形态下任一时刻只有一页在 DOM——
 * observer 没有可观测的多个区块，那段逻辑整段删除而非注释掉（DoD 明确点名它是死代码）。
 *
 * 当前项由**当前路由**决定，不再由点击设定内部 state。这消掉了一类不一致：浏览器后退时
 * 路由变了而内部 state 没变，高亮会停在上一页。
 *
 * **窄视口转抽屉**（R-04）。用 CSS 媒体查询控制布局，用一个受控的 open 状态控制抽屉的
 * 展开。不按视口宽度在 JS 里分支渲染：那需要监听 resize 并在测试里 mock 尺寸，而 jsdom
 * 的元素尺寸恒为 0，那类测试只会得到假绿灯。
 */

import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { HOME, NAV_ITEMS, NOT_READY_REASON, SETTINGS, analysisPath } from '../routes/paths'

type Tone = 'idle' | 'busy' | 'ok' | 'bad'

interface Props {
  /** 当前任务。空串表示还没有分析——导航项指向首页而非某次分析。 */
  taskId: string
  /** 分析结果是否就绪。决定依赖结果的导航项是否可点（R-03）。 */
  ready: boolean
  /** 顶部概览条。由路由层传入，外壳不关心它的内容（U10 填充）。 */
  topbar: ReactNode
  stageTone: Tone
  children: ReactNode
}

const DOT_TONE: Record<Tone, string> = {
  idle: '',
  busy: ' rail__dot--busy',
  ok: ' rail__dot--ok',
  bad: ' rail__dot--bad',
}

export function AppShell({ taskId, ready, topbar, stageTone, children }: Props) {
  const location = useLocation()
  const [drawerOpen, setDrawerOpen] = useState(false)
  // 记住上一个路径：只有真正换页时才收起抽屉。查看器改查询参数（换文件）不该收起它。
  const lastPathRef = useRef(location.pathname)

  useEffect(() => {
    if (lastPathRef.current !== location.pathname) {
      lastPathRef.current = location.pathname
      setDrawerOpen(false)
    }
  }, [location.pathname])

  return (
    <div className="shell">
      <button
        type="button"
        className="shell__drawer-toggle"
        aria-expanded={drawerOpen}
        aria-controls="rail-nav"
        onClick={() => setDrawerOpen((open) => !open)}
      >
        {drawerOpen ? '收起导航' : '展开导航'}
      </button>

      <aside className={drawerOpen ? 'rail rail--open' : 'rail'}>
        <div className="rail__brand">
          <span className="rail__mark" aria-hidden="true" />
          <span>
            <NavLink to={HOME} className="rail__name">
              CodePilot
            </NavLink>
            <span className="rail__tag">代码情报工作台</span>
          </span>
        </div>

        <nav className="rail__nav" id="rail-nav" aria-label="工作台导航">
          <span className="rail__label">工作区</span>
          {NAV_ITEMS.map((item) => {
            const blocked = item.needsResult && (!ready || !taskId)
            const to = item.absolute ?? (taskId ? analysisPath(taskId, item.page!) : HOME)

            if (blocked) {
              return (
                <button
                  key={item.id}
                  type="button"
                  className="rail__item"
                  disabled
                  // 原因放 title 与 aria-label，不做可见文案。
                  //
                  // 九项里有七项会同时置灰，每项都挂一句相同的说明就是七行重复噪声——
                  // owner 明确要求去掉：点不动本身已经说明了状态。悬停与读屏仍能取到原因，
                  // 所以 AE-01 的「标注原因」没有丢，只是不再占版面。
                  title={NOT_READY_REASON}
                  aria-label={`${item.label}（${NOT_READY_REASON}）`}
                >
                  {item.label}
                </button>
              )
            }

            return (
              <NavLink
                key={item.id}
                to={to}
                className={({ isActive }) =>
                  isActive ? 'rail__item rail__item--on' : 'rail__item'
                }
              >
                {item.label}
              </NavLink>
            )
          })}

          <span className="rail__label">设置</span>
          <NavLink
            to={SETTINGS}
            className={({ isActive }) =>
              isActive ? 'rail__item rail__item--on' : 'rail__item'
            }
          >
            Settings
          </NavLink>
        </nav>

        <div className="rail__foot">
          <span className="rail__phase">
            <span className={`rail__dot${DOT_TONE[stageTone]}`} aria-hidden="true" />
            {taskId ? <span className="rail__task">任务 {taskId}</span> : '尚无任务'}
          </span>
        </div>
      </aside>

      <div className="stage">
        <header className="topbar">{topbar}</header>
        <main className="canvas">
          <div className="column">{children}</div>
        </main>
      </div>
    </div>
  )
}
