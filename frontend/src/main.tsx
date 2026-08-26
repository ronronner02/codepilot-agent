import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { App } from './App'
import './styles.css'

const container = document.getElementById('root')
if (!container) {
  throw new Error('找不到 #root 挂载点')
}

createRoot(container).render(
  // StrictMode 在开发期会双调用 effect，能暴露 SSE 清理逻辑的缺陷——正是计划点名
  // 最容易出竞态的地方。留着它。
  <StrictMode>
    {/* Router 在这里而非 App 内部：测试要用 MemoryRouter 换掉它。 */}
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
