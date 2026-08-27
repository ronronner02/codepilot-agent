/**
 * 模块节点图（U11，R-09、R-14、R-64）。
 *
 * 节点是模块，边是跨模块依赖，方向为「导入方 → 被导入方」。**不渲染任何调用关系边**
 * （R-14、NA-05）——数据源里没有调用信息，画出来就是编的。
 *
 * 布局用 dagre 的从上到下分层：模块数通常小于 40，分层比力导向更稳定（同一份数据每次
 * 渲染位置一致，截图可复现）。力导向每次的随机初值会让「图变了吗」这个问题无法回答。
 *
 * **节点必须可 Tab 到达**（R-64）。React Flow 的默认节点不在 tab 序列里，所以这里用自定义
 * 节点组件显式给 `tabIndex` 与可见焦点态。这是选这个库的已知代价（KTD2）。
 *
 * jsdom 下的挂载：元素尺寸恒为 0 且没有 ResizeObserver（测试 setup 里 mock 了它）。组件能
 * 挂载不抛异常即可，图的正确性断言落在同页的等价文本表达上（KTD8）。
 */

import { useCallback, useMemo } from 'react'
import Dagre from '@dagrejs/dagre'
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { DependencyEdge, ModuleInfo } from '../api/types'

interface Props {
  modules: ModuleInfo[]
  edges: DependencyEdge[]
  selected: string | null
  onSelect: (name: string) => void
}

// 节点尺寸。dagre 要在布局前知道盒子大小，而真实尺寸要等渲染后才有——给一个与 CSS 一致的
// 固定值，让布局在首帧就正确。两处不一致的表现是节点重叠或间距过大。
const NODE_WIDTH = 190
const NODE_HEIGHT = 64

type ModuleNodeData = {
  label: string
  fileCount: number
  incomplete: boolean
  selected: boolean
  onSelect: (name: string) => void
}

/** 自定义节点：给 tabIndex 让它进 Tab 序列（R-64），回车与空格等价于点击。 */
function ModuleNode({ data }: NodeProps) {
  const payload = data as ModuleNodeData
  return (
    <div
      className={
        payload.selected ? 'graph-node graph-node--on' : 'graph-node'
      }
      tabIndex={0}
      role="button"
      aria-pressed={payload.selected}
      onClick={() => payload.onSelect(payload.label)}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          payload.onSelect(payload.label)
        }
      }}
    >
      <Handle type="target" position={Position.Top} />
      <span className="graph-node__name">{payload.label}</span>
      <span className="graph-node__meta">
        {payload.fileCount} 个文件
        {payload.incomplete ? ' · 分析不完整' : ''}
      </span>
      <Handle type="source" position={Position.Bottom} />
    </div>
  )
}

const NODE_TYPES = { module: ModuleNode }

function layout(modules: ModuleInfo[], edges: DependencyEdge[]): Map<string, { x: number; y: number }> {
  const graph = new Dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}))
  graph.setGraph({ rankdir: 'TB', nodesep: 40, ranksep: 70 })
  for (const module of modules) {
    graph.setNode(module.name, { width: NODE_WIDTH, height: NODE_HEIGHT })
  }
  for (const edge of edges) {
    // 只连两端都在模块清单里的边：聚合时可能留下指向已被过滤模块的边，
    // 交给 dagre 会让它凭空造一个节点。
    if (graph.hasNode(edge.source) && graph.hasNode(edge.target)) {
      graph.setEdge(edge.source, edge.target)
    }
  }
  Dagre.layout(graph)

  const positions = new Map<string, { x: number; y: number }>()
  for (const module of modules) {
    const node = graph.node(module.name)
    // dagre 给的是中心点，React Flow 要左上角。
    positions.set(module.name, {
      x: (node?.x ?? 0) - NODE_WIDTH / 2,
      y: (node?.y ?? 0) - NODE_HEIGHT / 2,
    })
  }
  return positions
}

export function ModuleGraph({ modules, edges, selected, onSelect }: Props) {
  const handleSelect = useCallback((name: string) => onSelect(name), [onSelect])

  const nodes = useMemo<Node[]>(() => {
    const positions = layout(modules, edges)
    return modules.map((module) => ({
      id: module.name,
      type: 'module',
      position: positions.get(module.name) ?? { x: 0, y: 0 },
      data: {
        label: module.name,
        fileCount: module.files.length,
        incomplete: module.limitation.length > 0,
        selected: module.name === selected,
        onSelect: handleSelect,
      } satisfies ModuleNodeData,
    }))
  }, [modules, edges, selected, handleSelect])

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((edge, index) => ({
        id: `${edge.source}->${edge.target}-${index}`,
        source: edge.source,
        target: edge.target,
        // 箭头由 React Flow 的默认 marker 提供，方向即数据方向。
        animated: false,
      })),
    [edges],
  )

  return (
    <div className="graph" aria-label="模块依赖节点图">
      <ReactFlow
        nodes={nodes}
        edges={flowEdges}
        nodeTypes={NODE_TYPES}
        fitView
        // 节点位置由 dagre 决定，不让用户拖动——拖动后的位置无处保存，刷新即丢失，
        // 而「我明明摆好了」是比不能拖更差的体验。
        nodesDraggable={false}
        nodesConnectable={false}
        proOptions={{ hideAttribution: false }}
      >
        <Background />
        <Controls />
      </ReactFlow>
    </div>
  )
}
