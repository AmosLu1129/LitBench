// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 模型 API 设置（BYO key）：让用户自己在界面里配 base_url / model / API key。
 *
 * 设计取舍：
 * - 三种协议（Chat Completions / Anthropic Messages / Responses）必须完整可见——名称、端点、
 *   适用场景都从后端 `supported_protocols` 读，前端不维护第二份，避免后端加协议后 UI 漂移；
 * - key 只提交给服务端，读回只有掩码；输入框留空 = 保留已保存的 key（这是后端契约）；
 * - 「测试连接」用当前表单值先测、不落库，失败就把后端给的中文原因原样展示出来。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { formatTimeWithSeconds } from './format'
import {
  clearLlmSettings,
  fetchLlmSettings,
  saveLlmSettings,
  testLlmSettings,
} from './api'
import type { LlmProtocolOption, LlmSettings, LlmSettingsInput, LlmTestResult } from './types'

interface Props {
  onClose: () => void
  onToast: (message: string) => void
  /** 保存/测试/清除后把最新配置回传给 App（顶栏状态灯用） */
  onChanged?: (settings: LlmSettings) => void
}

const SOURCE_LABEL: Record<string, { text: string; cls: string }> = {
  ui: { text: '界面配置', cls: 'ui' },
  env: { text: '环境变量', cls: 'env' },
  none: { text: '未配置', cls: 'none' },
}

/** Base URL 的按协议示例（只是 placeholder，协议定义仍以后端为准）。 */
const BASE_PLACEHOLDER: Record<string, string> = {
  chat_completion: 'https://api.deepseek.com 或 https://xxx/v1',
  anthropic_messages: 'https://api.anthropic.com',
  openai_responses: 'https://api.openai.com/v1',
}

/** 模型名示例，同样只是 placeholder。 */
const MODEL_PLACEHOLDER: Record<string, string> = {
  chat_completion: 'deepseek-chat / gpt-4o-mini / 任意兼容模型名',
  anthropic_messages: 'claude-sonnet-4-5',
  openai_responses: 'gpt-5',
}

export default function LlmSettings({ onClose, onToast, onChanged }: Props) {
  const panelRef = useRef<HTMLElement>(null)
  const [settings, setSettings] = useState<LlmSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  // 表单（初始值来自当前生效配置；未配置时给默认值）
  const [protocol, setProtocol] = useState('chat_completion')
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [timeoutS, setTimeoutS] = useState('60')
  const [showKey, setShowKey] = useState(false)

  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [clearing, setClearing] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<LlmTestResult | null>(null)

  const applySettings = useCallback(
    (data: LlmSettings) => {
      setSettings(data)
      setProtocol(data.protocol ?? data.supported_protocols[0]?.protocol ?? 'chat_completion')
      setBaseUrl(data.base_url ?? '')
      setModel(data.model ?? '')
      setTimeoutS(String(data.timeout_s ?? 60))
      setApiKey('')
      setShowKey(false)
      onChanged?.(data)
    },
    [onChanged],
  )

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      applySettings(await fetchLlmSettings())
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [applySettings])

  /** 只同步「状态」，不改表单（测试连接后调用，避免把用户没保存的输入重置掉）。 */
  const refreshStatus = useCallback(async () => {
    try {
      const data = await fetchLlmSettings()
      setSettings(data)
      onChanged?.(data)
    } catch {
      /* 状态同步失败不影响表单 */
    }
  }, [onChanged])

  useEffect(() => {
    void load()
  }, [load])

  // Esc 关闭 + 锁滚动（与系列浮层保持同一套交互）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    panelRef.current?.focus()
    document.body.classList.add('overlay-open')
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.classList.remove('overlay-open')
    }
  }, [onClose])

  const protocols: LlmProtocolOption[] = settings?.supported_protocols ?? []
  const activeOption = useMemo(
    () => protocols.find((p) => p.protocol === protocol) ?? protocols[0] ?? null,
    [protocols, protocol],
  )
  const source = SOURCE_LABEL[settings?.source ?? 'none'] ?? {
    text: settings?.source ?? '未配置',
    cls: 'none',
  }
  const keyPlaceholder = settings?.has_api_key
    ? `已保存（${settings.api_key_masked ?? 'sk-…'}），留空保持不变`
    : '粘贴 API Key'

  /** 表单 → 请求体；api_key 留空时不传（后端保留已保存的 key）。 */
  const buildInput = (): LlmSettingsInput => ({
    protocol,
    base_url: baseUrl.trim(),
    model: model.trim(),
    ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
    timeout_s: Number(timeoutS) || 60,
  })

  const validate = (): string | null => {
    if (!baseUrl.trim()) return '请填写 Base URL'
    if (!model.trim()) return '请填写 Model'
    if (!settings?.has_api_key && !apiKey.trim()) return '首次配置必须填写 API Key'
    const sec = Number(timeoutS)
    if (!Number.isFinite(sec) || sec < 5 || sec > 300) return '超时秒数需要在 5–300 之间'
    return null
  }

  const save = async () => {
    if (saving) return
    const problem = validate()
    if (problem) {
      setFormError(problem)
      return
    }
    setFormError(null)
    setSaving(true)
    try {
      const updated = await saveLlmSettings(buildInput())
      applySettings(updated)
      setTestResult(null)
      onToast('已保存，下一次生成章节摘要会用新模型')
    } catch (err) {
      setFormError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const test = async () => {
    if (testing) return
    const problem = validate()
    if (problem) {
      setFormError(problem)
      return
    }
    setFormError(null)
    setTesting(true)
    try {
      setTestResult(await testLlmSettings(buildInput()))
    } catch (err) {
      setTestResult({
        ok: false,
        error: err instanceof Error ? err.message : String(err),
        protocol,
        model: model.trim(),
        endpoint: '',
        latency_ms: null,
        reply: null,
        tokens_in: null,
        tokens_out: null,
      })
    } finally {
      setTesting(false)
      // 测试成功/失败会写回「已保存配置」的连接状态，同步给顶栏
      void refreshStatus()
    }
  }

  const clear = async () => {
    if (clearing) return
    if (!window.confirm('清除界面配置？已保存的 API Key 也会删除。')) return
    setClearing(true)
    setFormError(null)
    try {
      const updated = await clearLlmSettings()
      applySettings(updated)
      setTestResult(null)
      onToast('已清除界面配置')
    } catch (err) {
      setFormError(err instanceof Error ? err.message : String(err))
    } finally {
      setClearing(false)
    }
  }

  const busy = saving || testing || clearing || loading

  return (
    <div className="settings-backdrop" role="presentation" onPointerDown={onClose}>
      <section
        className="settings-panel"
        role="dialog"
        aria-modal="true"
        aria-label="模型 API 设置"
        tabIndex={-1}
        ref={panelRef}
        onPointerDown={(e) => e.stopPropagation()}
      >
        <header className="settings-head">
          <div>
            <h2>模型 API 设置</h2>
            <p className="settings-sub">
              由于 LitBench 需要使用模型的视觉能力，因此建议配置原生多模态模型。
            </p>
          </div>
          <button className="close" onClick={onClose} aria-label="关闭设置">
            ✕
          </button>
        </header>

        {loading && <p className="sum-muted">载入当前配置…</p>}
        {loadError && (
          <div className="banner error settings-load-error">
            <span>读取配置失败：{loadError}</span>
            <button className="ghost" onClick={() => void load()}>
              重试
            </button>
          </div>
        )}

        {settings && (
          <>
            {/* 当前生效 */}
            <section className="settings-now">
              <div className="settings-now-head">
                <span className={`source-badge ${source.cls}`}>{source.text}</span>
                <span className="settings-now-title">
                  {settings.configured
                    ? `${settings.protocol_label ?? settings.protocol ?? '已配置'}`
                    : '未配置模型 API'}
                </span>
                {settings.configured && (
                  <span
                    className={`conn-tag ${settings.last_test_ok ? 'ok' : 'bad'}`}
                    title={settings.last_test_ok ? '最近一次测试连接成功' : '最近一次测试连接未成功'}
                  >
                    <i className="conn-dot" aria-hidden="true" />
                    {settings.last_test_ok ? '已连接' : '未连接'}
                  </span>
                )}
                {settings.protocol_path && <code className="endpoint">{settings.protocol_path}</code>}
              </div>
              <dl className="settings-kv">
                <dt>Base URL</dt>
                <dd>{settings.base_url || <span className="muted">—</span>}</dd>
                <dt>Model</dt>
                <dd>{settings.model || <span className="muted">—</span>}</dd>
                <dt>API Key</dt>
                <dd>
                  {settings.has_api_key ? (
                    <code>{settings.api_key_masked ?? '已保存'}</code>
                  ) : (
                    <span className="muted">未配置</span>
                  )}
                </dd>
                <dt>实际端点</dt>
                <dd>{settings.endpoint ? <code>{settings.endpoint}</code> : <span className="muted">—</span>}</dd>
                <dt>更新时间</dt>
                <dd>{formatTimeWithSeconds(settings.updated_at)}</dd>
              </dl>
            </section>

            {/* 表单 */}
            <form
              className="settings-form"
              onSubmit={(e) => {
                e.preventDefault()
                void save()
              }}
            >
              <fieldset className="settings-fieldset">
                <legend>
                  协议 <span className="req">必选</span>
                </legend>
                <div className="proto-grid" role="radiogroup" aria-label="模型 API 协议">
                  {protocols.map((option) => {
                    const on = option.protocol === protocol
                    return (
                      <button
                        key={option.protocol}
                        type="button"
                        role="radio"
                        aria-checked={on}
                        className={`proto-card ${on ? 'on' : ''}`}
                        onClick={() => {
                          setProtocol(option.protocol)
                          setTestResult(null)
                        }}
                        disabled={busy}
                      >
                        <span className="proto-top">
                          <span className="proto-label">{option.label}</span>
                          <code className="proto-path">{option.path}</code>
                          <span className="proto-radio" aria-hidden="true">
                            {on ? '●' : '○'}
                          </span>
                        </span>
                      </button>
                    )
                  })}
                </div>
              </fieldset>

              <div className="settings-grid">
                <label className="settings-field">
                  <span>Base URL</span>
                  <input
                    value={baseUrl}
                    onChange={(e) => setBaseUrl(e.target.value)}
                    placeholder={BASE_PLACEHOLDER[protocol] ?? 'https://…'}
                    spellCheck={false}
                    maxLength={500}
                    disabled={busy}
                  />
                  <small>
                    {activeOption
                      ? `实际请求：${activeOption.path}（会拼在 Base URL 后面）`
                      : '会拼在协议端点前面'}
                  </small>
                </label>

                <label className="settings-field">
                  <span>Model</span>
                  <input
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    placeholder={MODEL_PLACEHOLDER[protocol] ?? '模型名'}
                    spellCheck={false}
                    maxLength={200}
                    disabled={busy}
                  />
                  <small>大小写敏感</small>
                </label>
              </div>

              <div className="settings-grid">
                <label className="settings-field">
                  <span>API Key</span>
                  <span className="key-wrap">
                    <input
                      type={showKey ? 'text' : 'password'}
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      placeholder={keyPlaceholder}
                      spellCheck={false}
                      autoComplete="off"
                      maxLength={500}
                      disabled={busy}
                    />
                    <button
                      type="button"
                      className="link key-toggle"
                      onClick={() => setShowKey((v) => !v)}
                      disabled={busy}
                      aria-label={showKey ? '隐藏 API Key' : '显示 API Key'}
                    >
                      {showKey ? '隐藏' : '显示'}
                    </button>
                  </span>
                </label>

                <label className="settings-field settings-timeout">
                  <span>超时（秒）</span>
                  <input
                    type="number"
                    min={5}
                    max={300}
                    value={timeoutS}
                    onChange={(e) => setTimeoutS(e.target.value)}
                    disabled={busy}
                  />
                  <small>5–300，默认 60</small>
                </label>
              </div>

              {formError && (
                <div className="banner error settings-inline-error" role="alert">
                  {formError}
                </div>
              )}

              {testResult && (
                <div
                  className={`settings-test ${testResult.ok ? 'ok' : 'bad'}`}
                  role="status"
                  aria-live="polite"
                >
                  {testResult.ok ? (
                    <>
                      <strong>连接成功</strong>
                      <span>· {testResult.latency_ms ?? '—'}ms</span>
                      {testResult.reply && <span>· 返回：{testResult.reply}</span>}
                      {testResult.endpoint && <code>{testResult.endpoint}</code>}
                      {(testResult.tokens_in != null || testResult.tokens_out != null) && (
                        <span>
                          · tokens {testResult.tokens_in ?? '—'}/{testResult.tokens_out ?? '—'}
                        </span>
                      )}
                    </>
                  ) : (
                    <>
                      <strong>连接失败</strong>
                      <span>{testResult.error ?? '未知错误'}</span>
                    </>
                  )}
                </div>
              )}

              <div className="settings-actions">
                <button type="submit" className="primary" disabled={busy}>
                  {saving ? '保存中…' : '保存配置'}
                </button>
                <button
                  type="button"
                  className="ghost"
                  onClick={() => void test()}
                  disabled={busy}
                  title="用当前表单值测试，不会保存"
                >
                  {testing ? '测试中…' : '测试连接'}
                </button>
                {settings.source === 'ui' && (
                  <button
                    type="button"
                    className="ghost danger"
                    onClick={() => void clear()}
                    disabled={busy}
                    title="删除界面配置，回落到环境变量 / mock"
                  >
                    {clearing ? '清除中…' : '清除配置'}
                  </button>
                )}
                <span className="spacer" />
                <button type="button" className="ghost" onClick={onClose} disabled={saving}>
                  关闭
                </button>
              </div>
            </form>
          </>
        )}
      </section>
    </div>
  )
}
