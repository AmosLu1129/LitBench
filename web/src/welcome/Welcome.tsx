// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 欢迎页外壳：DOM 排版（竖排题字 / 朱印 / CTA）+ three.js 场景生命周期。
 * ?t=16&still=1 可冻结到某一秒，供截图与调构图。
 */
import { useEffect, useRef, useState } from 'react'
import { WelcomeScene } from './scene'
import type { WelcomePhase } from './scene'
import { WelcomeAudio } from './audio'
import './welcome.css'

const SEEN_KEY = 'litbench.welcome.seen'
const MUTED_KEY = 'litbench.welcome.muted'

export default function Welcome() {
  const hostRef = useRef<HTMLDivElement>(null)
  const sceneRef = useRef<WelcomeScene | null>(null)
  const audioRef = useRef<WelcomeAudio>(new WelcomeAudio(localStorage.getItem(MUTED_KEY) === '1'))

  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)
  const [done, setDone] = useState(false)
  const [leaving, setLeaving] = useState(false)
  const [muted, setMuted] = useState(() => localStorage.getItem(MUTED_KEY) === '1')
  const [armed, setArmed] = useState(false)
  const [phase, setPhase] = useState<WelcomePhase>('intro')

  // 场景
  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const params = new URLSearchParams(window.location.search)
    const atRaw = params.get('t')
    const at = atRaw === null ? undefined : Number(atRaw)
    let ctrl: WelcomeScene | null = null
    try {
      ctrl = new WelcomeScene(host, {
        startAt: Number.isFinite(at) ? at : undefined,
        frozen: params.get('still') === '1' || Number.isFinite(at),
        onDone: () => setDone(true),
        onEvent: (name) => audioRef.current.play(name),
        onPhase: (p) => setPhase(p),
      })
      sceneRef.current = ctrl
      setReady(true)
    } catch (err) {
      console.error('[LitBench] 欢迎页 WebGL 初始化失败，改用静态画面', err)
      setFailed(true)
    }
    return () => {
      ctrl?.dispose()
      sceneRef.current = null
    }
  }, [])

  // 第一次用户手势里解锁音频（浏览器策略）
  useEffect(() => {
    const arm = () => {
      if (localStorage.getItem(MUTED_KEY) === '1') return
      void audioRef.current.enable().then((ok) => {
        if (ok) setArmed(true)
      })
    }
    window.addEventListener('pointerdown', arm, { once: true })
    window.addEventListener('keydown', arm, { once: true })
    return () => {
      window.removeEventListener('pointerdown', arm)
      window.removeEventListener('keydown', arm)
    }
  }, [])

  const enter = () => {
    if (leaving) return
    localStorage.setItem(SEEN_KEY, '1')
    setLeaving(true)
    window.setTimeout(() => window.location.replace('/'), 750)
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') sceneRef.current?.skipToEnd()
      if (e.key === 'Enter') enter()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const toggleSound = () => {
    const next = !muted
    setMuted(next)
    localStorage.setItem(MUTED_KEY, next ? '1' : '0')
    audioRef.current.setMuted(next)
    if (!next) {
      void audioRef.current.enable().then((ok) => {
        if (ok) setArmed(true)
      })
    }
  }

  const replay = () => {
    setDone(false)
    sceneRef.current?.seek(0)
    sceneRef.current?.start()
  }

  return (
    <div className={`welcome${ready ? ' is-ready' : ''}${leaving ? ' is-leaving' : ''}`} data-phase={phase}>
      <div className="welcome-stage" ref={hostRef} aria-hidden="true" />

      {failed && <div className="welcome-static" aria-hidden="true" />}
      <div className="welcome-veil" aria-hidden="true" />

      <div className="welcome-ui">
        <div className="welcome-col">
          <h1 className="welcome-title">循光至页</h1>
          <div className="welcome-sign">LitBench · 文献证据工作台</div>
          <div className="welcome-seal" aria-label="可核">
            可核
          </div>
        </div>

        <p className="welcome-sub">每一句话，都能回到它出生的那一页。</p>

        <div className="welcome-actions">
          {done ? (
            <button type="button" className="welcome-btn" onClick={replay}>
              重看开窗
            </button>
          ) : (
            <button type="button" className="welcome-btn" onClick={() => sceneRef.current?.skipToEnd()}>
              跳过
            </button>
          )}
          <button type="button" className="welcome-enter" onClick={enter}>
            展卷 <span aria-hidden="true">→</span>
          </button>
        </div>

        <div className="welcome-foot">10 篇 · 5,695 段 · 140 图 · 19 表　本机测试语料</div>

        <button
          type="button"
          className={`welcome-sound${armed && !muted ? ' is-on' : ''}`}
          onClick={toggleSound}
          aria-label={muted ? '开启环境音' : '关闭环境音'}
          title={muted ? '开启环境音' : '关闭环境音'}
        >
          {muted ? '静' : '声'}
        </button>

        {!armed && !muted && <div className="welcome-hint">点一下，听开窗的声音</div>}
      </div>

      <div className="welcome-light" aria-hidden="true" />
    </div>
  )
}
