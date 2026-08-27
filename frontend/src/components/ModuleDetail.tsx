/**
 * 节点详情侧栏（U11，R-10、R-11）。
 *
 * **`summary` 直接渲染原文，不做任何加工**（R-11）。不摘要、不截断、不换行重排——一旦这里
 * 动了字，界面就无法声称「这是模型给出的结论」。这条约束在实现上表现为：整个组件里没有
 * 任何对 summary 的字符串操作。
 *
 * `limitation` 非空表示该模块分析不完整，必须标注——不标注会让一份残缺的结论看起来和完整
 * 的一样可信。
 *
 * 成员文件路径可点，跳到查看器（R-12、U13）。
 */

import { CitationLink } from './CitationLink'
import type { ModuleInfo } from '../api/types'

interface Props {
  module: ModuleInfo | null
  taskId: string
}

export function ModuleDetail({ module, taskId }: Props) {
  if (module === null) {
    return (
      <aside className="module-detail module-detail--empty">
        <p>点击左侧节点或下方清单中的模块名，这里显示它的成员文件与分析结论。</p>
      </aside>
    )
  }

  return (
    <aside className="module-detail" aria-labelledby="module-detail-heading">
      <h2 className="module-detail__heading" id="module-detail-heading">
        {module.name}
      </h2>

      <dl className="module-detail__stats">
        <div>
          <dt>成员文件</dt>
          <dd>{module.files.length}</dd>
        </div>
        <div>
          <dt>内部边</dt>
          <dd>{module.internal_edges}</dd>
        </div>
        <div>
          <dt>外部边</dt>
          <dd>{module.external_edges}</dd>
        </div>
        <div>
          <dt>聚类来历</dt>
          <dd>{module.origin}</dd>
        </div>
      </dl>

      <h3 className="module-detail__section">职责</h3>
      {module.summary ? (
        // 原文直传（R-11）。这里做任何加工，就无法声称结论未被改写。
        <p className="module-detail__summary">{module.summary}</p>
      ) : (
        <p className="module-detail__summary module-detail__summary--absent">
          本模块未被深挖分析。它仍出现在结构图里，但没有分析结论。
        </p>
      )}

      {module.limitation ? (
        <p className="module-detail__limitation" role="status">
          该模块分析不完整：{module.limitation}
        </p>
      ) : null}

      <h3 className="module-detail__section">成员文件</h3>
      <ul className="module-detail__files">
        {module.files.map((path) => (
          <li key={path}>
            <CitationLink taskId={taskId} path={path} />
          </li>
        ))}
      </ul>
    </aside>
  )
}
