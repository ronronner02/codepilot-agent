/**
 * Architecture 页（U11，R-09~R-14、R-66）。
 *
 * 节点图 + 节点详情侧栏 + 等价文本表达（模块清单与依赖关系表）。
 *
 * **文件级边要聚合到模块级。** `dependency_graph.edges` 是文件到文件的，而图上的节点是模块
 * ——按成员归属把边折叠成模块间边，方向保留，去重，并丢掉自环（模块内部的依赖已经由
 * `internal_edges` 表达，画成自环只是噪声）。
 *
 * **降级要显式标注**（R-13）。`granularity` 为 directory 时在图上方说明降级及原因，不静默
 * 以粗粒度呈现——否则读者会把目录级的粗略结构当成文件级的精确结构。
 */

import { useMemo, useState } from 'react'
import { DependencyTable } from '../components/DependencyTable'
import { ModuleDetail } from '../components/ModuleDetail'
import { ModuleGraph } from '../components/ModuleGraph'
import type { DependencyEdge, ModuleInfo } from '../api/types'
import { useAnalysis } from '../state/analysis'
import { usePageHeading } from './usePageHeading'

/** 把文件级边折叠成模块级边。方向保留，去重，丢自环。 */
export function aggregateEdges(
  modules: ModuleInfo[],
  fileEdges: DependencyEdge[],
): DependencyEdge[] {
  const owner = new Map<string, string>()
  for (const module of modules) {
    for (const file of module.files) owner.set(file, module.name)
  }

  const seen = new Set<string>()
  const result: DependencyEdge[] = []
  for (const edge of fileEdges) {
    const source = owner.get(edge.source)
    const target = owner.get(edge.target)
    // 两端都要能归属到模块：解析缺口留下的边指向不存在的成员，画它等于画一条假边。
    if (!source || !target || source === target) continue
    const key = `${source}->${target}`
    if (seen.has(key)) continue
    seen.add(key)
    result.push({ source, target })
  }
  return result
}

export function ArchitecturePage() {
  const headingRef = usePageHeading<HTMLHeadingElement>()
  const { result, taskId, ready } = useAnalysis()
  const [selected, setSelected] = useState<string | null>(null)

  const modules = result?.modules ?? []
  const graph = result?.dependency_graph ?? null
  const moduleEdges = useMemo(
    () => aggregateEdges(modules, graph?.edges ?? []),
    [modules, graph],
  )
  const selectedModule = modules.find((m) => m.name === selected) ?? null

  if (!ready || !result) {
    return (
      <section className="page">
        <h1 className="page__title" ref={headingRef} tabIndex={-1}>
          Architecture
        </h1>
        <p className="page__empty">分析结果尚未就绪。完成一次分析后可查看模块结构。</p>
      </section>
    )
  }

  if (modules.length === 0) {
    return (
      <section className="page">
        <h1 className="page__title" ref={headingRef} tabIndex={-1}>
          Architecture
        </h1>
        <p className="page__empty">
          本次分析未产出模块划分。
        </p>
      </section>
    )
  }

  return (
    <section className="page">
      <h1 className="page__title" ref={headingRef} tabIndex={-1}>
        Architecture
      </h1>

      {graph?.degraded_reason ? (
        <p className="page__note" role="status">
          依赖图降级为目录级粒度：{graph.degraded_reason}
          。图上的节点代表目录而非精确的模块聚类。
        </p>
      ) : null}

      {/* 不写「边的方向是导入方→被导入方」这类说明：依赖关系表里每行都是「A 导入 B」，
          方向自己就读得出来。也不写「没有调用链」——NA-05 要的是不画，不是声明不画。 */}
      <div className="arch">
        <ModuleGraph
          modules={modules}
          edges={moduleEdges}
          selected={selected}
          onSelect={setSelected}
        />
        <ModuleDetail module={selectedModule} taskId={taskId} />
      </div>

      <DependencyTable
        modules={modules}
        edges={moduleEdges}
        selected={selected}
        onSelect={setSelected}
      />

      {graph && graph.unresolved.length > 0 ? (
        <details className="page__note">
          <summary>{graph.unresolved.length} 个 import 未能解析到仓库内文件</summary>
          <ul>
            {graph.unresolved.slice(0, 20).map((item, index) => (
              <li key={`${item.path}-${item.line}-${index}`}>
                <code>
                  {item.path}:{item.line}
                </code>{' '}
                → {item.target}（{item.reason}）
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  )
}
