// SPDX-License-Identifier: AGPL-3.0-or-later
import { Component, StrictMode, Suspense, lazy } from 'react'
import type { CSSProperties, ErrorInfo, ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import AuthGate from './AuthGate'
// 自托管字体（OFL）：Fraunces 拉丁展示衬线 + Noto Serif SC 中文宋体 + JetBrains Mono 数字/引用键
import '@fontsource/fraunces/latin-400.css'
import '@fontsource/fraunces/latin-500.css'
import '@fontsource/fraunces/latin-600.css'
import '@fontsource/fraunces/latin-700.css'
import '@fontsource/noto-serif-sc/400.css'
import '@fontsource/noto-serif-sc/500.css'
import '@fontsource/noto-serif-sc/600.css'
import '@fontsource/noto-serif-sc/700.css'
import '@fontsource/jetbrains-mono/latin-400.css'
import '@fontsource/jetbrains-mono/latin-500.css'
import './styles.css'

/**
 * 入口分流：首访进欢迎页（three.js 全部落在懒加载 chunk，工作台首屏体积不变），
 * 之后直接进工作台。`?welcome=1` 可强制回看序厅，`?welcome=0` 可跳过。
 */
const Welcome = lazy(() => import('./welcome/Welcome'))
const App = lazy(() => import('./App'))

function pickRoot() {
  const force = new URLSearchParams(window.location.search).get('welcome')
  const seen = localStorage.getItem('litbench.welcome.seen') === '1'
  return force === '1' || (force !== '0' && !seen) ? Welcome : App
}

/**
 * 渲染出异常时别给用户一片白屏：显示可读错误 + 刷新按钮，并保留错误详情方便排查。
 * 这里刻意不依赖 styles.css 的类（样式批 F 正在清理），用内联样式保证任何情况下都能显示。
 */
const SHELL_STYLE: CSSProperties = {
  maxWidth: 720,
  margin: '48px auto',
  padding: '0 24px',
  fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif',
  lineHeight: 1.7,
  color: '#2b2b2b',
}

const PRE_STYLE: CSSProperties = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  background: '#f5f0e6',
  border: '1px solid #ddd3c2',
  borderRadius: 8,
  padding: '12px 14px',
  fontSize: 13,
  maxHeight: 320,
  overflow: 'auto',
}

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[LitBench] 界面渲染出错', error, info.componentStack)
  }

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div style={SHELL_STYLE} role="alert">
        <h2 style={{ margin: '0 0 10px' }}>界面渲染出错</h2>
        <p style={{ margin: '0 0 14px', color: '#6b6255' }}>
          这一步的界面没能显示出来，已保存的数据不会丢。可以刷新页面重试；如果反复出现，
          把下面的错误信息发给维护者。
        </p>
        <pre style={PRE_STYLE}>{error.message || String(error)}</pre>
        <button
          type="button"
          onClick={() => window.location.reload()}
          style={{
            marginTop: 16,
            padding: '8px 16px',
            fontSize: 14,
            borderRadius: 8,
            border: '1px solid #b9a98c',
            background: '#f7f2e8',
            cursor: 'pointer',
          }}
        >
          刷新页面
        </button>
      </div>
    )
  }
}

const root = document.getElementById('root')
if (!root) throw new Error('#root not found')

const Root = pickRoot()
const isWelcome = Root === Welcome

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <Suspense fallback={null}>
        {isWelcome ? <Root /> : <AuthGate><Root /></AuthGate>}
      </Suspense>
    </ErrorBoundary>
  </StrictMode>,
)
