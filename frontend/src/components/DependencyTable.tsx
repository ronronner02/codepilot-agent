/**
 * 节点图的等价文本表达（U11，R-66、BR-006）。
 *
 * **这份文本不是图的降级替代，而是图的正确性来源。** jsdom 没有布局引擎（元素尺寸恒为 0），
 * React Flow 依赖容器尺寸决定渲染——硬测渲染结果只会得到一批「通过但没验证任何东西」的
 * 绿灯（KTD8）。BR-006 本来就要求有等价文本，那份文本是真实可断言的产出：模块数、边的
 * 方向、降级标注、limitation 标注全部在其中。
 *
 * 图形本身的可用性（缩放平移、布局可读）由浏览器实跑核对承担，不由测试声称。
 */

import type { DependencyEdge, ModuleInfo } from '../api/types'

interface Props {
  modules: ModuleInfo[]
  /** 已聚合到模块级的边。方向是「导入方 → 被导入方」。 */
  edges: DependencyEdge[]
  /**
   * 选中某模块，与点击图节点等效。
   *
   * **这不只是为了可测。** 图节点在无布局环境（jsdom）与读屏软件下都不可达，如果只有图能
   * 打开详情，键盘用户就永远看不到模块结论。清单里的模块名可点让详情有一条不依赖图的
   * 通路——BR-006 要求等价文本，「等价」也包括能做同样的操作。
   */
  onSelect?: (name: string) => void
  selected?: string | null
}

export function DependencyTable({ modules, edges, onSelect, selected }: Props) {
  return (
    <div className="dep-text">
      <h2 className="page__section-title">模块清单</h2>
      <table className="dep-text__table">
        <caption className="dep-text__caption">
          共 {modules.length} 个模块。这份清单与上方节点图内容等价。
        </caption>
        <thead>
          <tr>
            <th scope="col">模块</th>
            <th scope="col">成员数</th>
            <th scope="col">内部边</th>
            <th scope="col">外部边</th>
            <th scope="col">来历</th>
          </tr>
        </thead>
        <tbody>
          {modules.map((module) => (
            <tr key={module.name}>
              <th scope="row">
                {onSelect ? (
                  <button
                    type="button"
                    className={
                      module.name === selected
                        ? 'dep-text__pick dep-text__pick--on'
                        : 'dep-text__pick'
                    }
                    aria-pressed={module.name === selected}
                    onClick={() => onSelect(module.name)}
                  >
                    {module.name}
                  </button>
                ) : (
                  module.name
                )}
              </th>
              <td>{module.files.length}</td>
              <td>{module.internal_edges}</td>
              <td>{module.external_edges}</td>
              <td>{module.origin}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2 className="page__section-title">依赖关系</h2>
      {edges.length === 0 ? (
        <p className="page__empty">模块之间没有跨模块依赖。</p>
      ) : (
        <ul className="dep-text__edges">
          {edges.map((edge, index) => (
            <li key={`${edge.source}->${edge.target}-${index}`}>
              {/* 方向要在文本里可读，不只靠箭头符号——「导入」两个字让方向不可误读。 */}
              <code>{edge.source}</code> 导入 <code>{edge.target}</code>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
