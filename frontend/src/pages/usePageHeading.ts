/**
 * 切页后的焦点转移与播报（R-65）。
 *
 * 多页形态下，点击导航后 DOM 换了一整片，而键盘与读屏用户的焦点仍停在导航项上——他们
 * 不知道内容变了。把焦点移到新页主标题是既有模式（`ProgressPanel`、`ReportView` 的
 * `focusOnMount` 已建立），这里把它抽成 hook 让九个页面共用。
 *
 * 标题要能接收焦点，所以调用方须给它 `tabIndex={-1}`：`-1` 表示「不进 Tab 序列但可被
 * 程序聚焦」。给 `0` 会让每个页面标题都插进 Tab 序列，Tab 一遍要多按一次。
 */

import { useEffect, useRef } from 'react'

export function usePageHeading<T extends HTMLElement>(): React.RefObject<T | null> {
  const ref = useRef<T>(null)

  useEffect(() => {
    ref.current?.focus()
    // 只在挂载时聚焦。依赖数组为空即「换页时才跑」——页面内的状态变化（换文件、提交检索）
    // 不该把焦点从用户当前操作的控件上抢走。
  }, [])

  return ref
}
