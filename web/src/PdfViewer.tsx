// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * PDF 原文查看器 + bbox overlay + **拖框修正**。
 *
 * 这是整个产品的「证据锚定」：用户看到缩略图之后，必须能一眼确认
 * 「这张图确实在原文这一页的这个地方」，否则引用就不可信。看不出来的时候，
 * 用户得能直接画一个对的框——这是纠错闭环的唯一人工入口。
 *
 * 坐标约定：后端给的 bbox 是 PDF 点、左上原点；PDF.js 的 viewport
 * 默认也是左上原点，所以只要乘 scale，不需要翻 y。
 * 反过来，鼠标坐标（CSS px）除以同一个 scale 就是 PDF 点。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'
import * as pdfjsLib from 'pdfjs-dist'
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFPageProxy,
  RenderTask,
} from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { reviewFigure, createFigure, describeHttpError } from './api'
import type { Figure } from './types'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

interface Props {
  /** 已有图才传；纯「框选加图」模式为 null */
  figureId: string | null
  paperId: string
  page: number
  bbox: number[] | null
  title: string
  label: string | null
  onClose: () => void
  onCorrected?: (fig: Figure) => void
  /** 允许在页面上框选新图（补解析器漏掉的图） */
  allowAdd?: boolean
  onAdded?: (fig: Figure) => void
}

/** 拖框的 CSS 像素坐标（相对页面左上角）。 */
type Draft = { x0: number; y0: number; x1: number; y1: number }

const MIN_DRAG_PX = 8

/** pdfjs 主动 cancel 渲染任务时会抛 RenderingCancelledException：这不是错误，直接忽略。 */
function isRenderingCancelled(err: unknown): boolean {
  if (err instanceof pdfjsLib.RenderingCancelledException) return true
  return (
    err instanceof Error &&
    (err.name === 'RenderingCancelledException' || err.message.includes('Rendering cancelled'))
  )
}

export default function PdfViewer({
  figureId,
  paperId,
  page,
  bbox,
  title,
  label,
  onClose,
  onCorrected,
  allowAdd = false,
  onAdded,
}: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const docRef = useRef<PDFDocumentProxy | null>(null)
  const taskRef = useRef<PDFDocumentLoadingTask | null>(null)
  // 同一时刻只保留一个渲染任务：翻页/重渲染前 cancel 上一个，
  // 否则 pdfjs 会报 "Cannot use the same canvas during multiple render() operations"。
  const renderTaskRef = useRef<RenderTask | null>(null)
  const pageNoRef = useRef(page)
  const [pageNo, setPageNo] = useState(page)
  const [pageCount, setPageCount] = useState(0)
  const [scale, setScale] = useState(1)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [drag, setDrag] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  // 刚保存的修正框：父组件的 bbox 更新前也能立刻显示
  const [localBbox, setLocalBbox] = useState<number[] | null>(null)
  // 「框选加图」：没有 figureId 时默认进入加图模式
  const [addMode, setAddMode] = useState(allowAdd && !figureId)
  const [pendingAdd, setPendingAdd] = useState<number[] | null>(null)
  const [addLabel, setAddLabel] = useState('')
  const [addCaption, setAddCaption] = useState('')

  const readOnly = !figureId && !allowAdd

  // 父组件切到别的页（点另一条证据 / 另一张图）时同步当前页
  useEffect(() => {
    setPageNo(page)
  }, [page])

  // 给异步渲染结果做「发起时的页码」快照，await 回来后判断是否已经翻页
  useEffect(() => {
    pageNoRef.current = pageNo
  }, [pageNo])

  useEffect(() => {
    setLocalBbox(null)
    setPendingAdd(null)
  }, [bbox, pageNo])

  // 载入 PDF（只载一次；错误后点重试会换 reloadKey 重载）
  useEffect(() => {
    let cancelled = false
    setStatus('loading')
    setError(null)
    ;(async () => {
      try {
        const res = await fetch(`/api/papers/${paperId}/pdf`)
        // 后端错误体（含 Pydantic 422 数组 detail）统一解析成可读文本
        if (!res.ok) throw new Error(await describeHttpError(res))
        const data = new Uint8Array(await res.arrayBuffer())
        const task = pdfjsLib.getDocument({ data })
        taskRef.current = task
        const doc = await task.promise
        if (cancelled) return
        docRef.current = doc
        setPageCount(doc.numPages)
        setStatus('ready')
      } catch (err) {
        if (cancelled) return
        setError(err instanceof Error ? err.message : String(err))
        setStatus('error')
      }
    })()
    return () => {
      cancelled = true
      // 渲染任务和文档任务都要收掉；pdfjs 6 里销毁入口在 loading task 上
      renderTaskRef.current?.cancel()
      renderTaskRef.current = null
      void taskRef.current?.destroy()
      taskRef.current = null
      docRef.current = null
    }
  }, [paperId, reloadKey])

  // 渲染当前页
  const renderPage = useCallback(async () => {
    const doc = docRef.current
    const canvas = canvasRef.current
    if (!doc || !canvas) return
    // 翻页 / 重渲染前先取消上一个渲染任务
    renderTaskRef.current?.cancel()
    renderTaskRef.current = null
    const targetPage = pageNo
    try {
      const pdfPage: PDFPageProxy = await doc.getPage(targetPage)
      // await 期间又翻页了：交给新的渲染，旧结果丢弃
      if (pageNoRef.current !== targetPage) return
      const base = pdfPage.getViewport({ scale: 1 })
      const avail = (wrapRef.current?.clientWidth ?? 900) - 32
      const nextScale = Math.min(avail / base.width, 2)
      const viewport = pdfPage.getViewport({ scale: nextScale })
      const dpr = window.devicePixelRatio || 1

      canvas.width = Math.floor(viewport.width * dpr)
      canvas.height = Math.floor(viewport.height * dpr)
      canvas.style.width = `${viewport.width}px`
      canvas.style.height = `${viewport.height}px`

      const ctx = canvas.getContext('2d')
      if (!ctx) return
      const params = {
        canvasContext: ctx,
        viewport,
        canvas,
        transform: dpr === 1 ? undefined : [dpr, 0, 0, dpr, 0, 0],
      } as unknown as Parameters<PDFPageProxy['render']>[0]
      const task = pdfPage.render(params)
      renderTaskRef.current = task
      await task.promise
      renderTaskRef.current = null
      setScale(nextScale)
      setError(null)
      setStatus('ready')
    } catch (err) {
      // 主动 cancel 触发的异常不是错误，不要显示到界面上
      if (isRenderingCancelled(err)) return
      if (pageNoRef.current !== targetPage) return
      setError(err instanceof Error ? err.message : String(err))
      setStatus('error')
    }
  }, [pageNo])

  useEffect(() => {
    if (status === 'ready') void renderPage()
  }, [status, renderPage])

  // 卸载时收掉正在跑的渲染任务
  useEffect(
    () => () => {
      renderTaskRef.current?.cancel()
      renderTaskRef.current = null
    },
    [],
  )

  /** 错误态的重试：PDF 没载入就重载文档，只是这一页渲染失败就重新渲染。 */
  const retry = useCallback(() => {
    setError(null)
    if (!docRef.current) {
      setStatus('loading')
      setReloadKey((key) => key + 1)
      return
    }
    // status: error → ready 会触发上面的渲染 effect 再跑一次
    setStatus('ready')
  }, [])

  // 键盘：Esc 关闭，左右翻页；输入框（加图表单 / 图号输入）聚焦时不抢按键
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null
      const tag = target?.tagName
      const typing =
        tag === 'INPUT' ||
        tag === 'TEXTAREA' ||
        tag === 'SELECT' ||
        target?.isContentEditable === true
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (typing) return
      if (e.key === 'ArrowLeft') setPageNo((n) => Math.max(1, n - 1))
      if (e.key === 'ArrowRight') setPageNo((n) => Math.min(pageCount || n, n + 1))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, pageCount])

  const effectiveBbox = localBbox ?? bbox
  const box =
    effectiveBbox && effectiveBbox.length === 4
      ? {
          left: effectiveBbox[0] * scale,
          top: effectiveBbox[1] * scale,
          width: Math.max((effectiveBbox[2] - effectiveBbox[0]) * scale, 2),
          height: Math.max((effectiveBbox[3] - effectiveBbox[1]) * scale, 2),
        }
      : null

  // ---- 拖框修正 -------------------------------------------------------
  const localPoint = (e: ReactPointerEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    return { x: e.clientX - rect.left, y: e.clientY - rect.top }
  }

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    // 只读模式（纯看证据）不做任何框选交互
    if (readOnly || status !== 'ready' || saving) return
    try {
      // 拖出画布范围时也要继续收到 move；合成事件（自动化测试）拿不到有效 pointerId
      e.currentTarget.setPointerCapture(e.pointerId)
    } catch {
      /* 没有指针捕获也能拖，只是拖出边界会断 */
    }
    const p = localPoint(e)
    setNotice(null)
    setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y })
  }

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!drag) return
    const p = localPoint(e)
    setDrag((d) => (d ? { ...d, x1: p.x, y1: p.y } : d))
  }

  const onPointerUp = async (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag
    if (!d) return
    const p = localPoint(e)
    setDrag(null)

    const x0 = Math.min(d.x0, p.x)
    const y0 = Math.min(d.y0, p.y)
    const x1 = Math.max(d.x0, p.x)
    const y1 = Math.max(d.y0, p.y)
    if (x1 - x0 < MIN_DRAG_PX || y1 - y0 < MIN_DRAG_PX) {
      setNotice('框太小了，重新拖一次')
      return
    }

    // CSS px → PDF 点：除以同一个 viewport scale，再夹到页面范围内
    const canvas = canvasRef.current
    const pageW = (canvas?.clientWidth ?? 0) / scale
    const pageH = (canvas?.clientHeight ?? 0) / scale
    const clamp = (v: number, hi: number) => Math.max(0, Math.min(v, hi))
    const boxPt = [
      Math.round(clamp(x0 / scale, pageW) * 10) / 10,
      Math.round(clamp(y0 / scale, pageH) * 10) / 10,
      Math.round(clamp(x1 / scale, pageW) * 10) / 10,
      Math.round(clamp(y1 / scale, pageH) * 10) / 10,
    ]

    // 加图模式：只记下框，等用户填图号/图注后保存
    if (addMode) {
      setPendingAdd(boxPt)
      setNotice('框已记录：填图号后点「添加这张图」')
      return
    }
    if (!figureId) return

    setSaving(true)
    try {
      const updated = await reviewFigure(figureId, 'wrong', {
        bbox: boxPt,
        note: '原文中拖框修正',
        reviewer: 'human',
      })
      setLocalBbox(updated.bbox && updated.bbox.length === 4 ? updated.bbox : boxPt)
      setNotice('已保存修正框，缩略图已重裁')
      onCorrected?.(updated)
    } catch (err) {
      setNotice(err instanceof Error ? `保存失败：${err.message}` : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const saveAdded = async () => {
    if (!pendingAdd || saving) return
    setSaving(true)
    setNotice(null)
    try {
      const fig = await createFigure({
        paper_id: paperId,
        page: pageNo,
        bbox: pendingAdd,
        label: addLabel.trim(),
        caption: addCaption.trim(),
      })
      setPendingAdd(null)
      setAddLabel('')
      setAddCaption('')
      setNotice(`已添加：${fig.label_raw ?? '无编号图'}（p.${fig.page}）`)
      onAdded?.(fig)
    } catch (err) {
      setNotice(err instanceof Error ? `添加失败：${err.message}` : '添加失败')
    } finally {
      setSaving(false)
    }
  }

  const dragHint = addMode ? '框选新图：在页面上拖一个框' : readOnly ? '' : '在页面上拖框可修正'

  return (
    <div className="viewer" role="dialog" aria-modal="true" aria-label="原文查看">
      <div className="viewer-bar">
        <span className="viewer-title" title={title}>
          {title}
        </span>
        {label && <span className="label">{label}</span>}
        <span className={`viewer-hint ${saving ? 'busy' : ''} ${notice ? 'on' : ''}`}>
          {saving ? '保存中…' : (notice ?? dragHint)}
        </span>
        <span className="spacer" />
        {allowAdd && (
          <button
            className={`ghost ${addMode ? 'on' : ''}`}
            onClick={() => {
              setAddMode((v) => !v)
              setPendingAdd(null)
              setNotice(null)
            }}
          >
            {addMode ? '退出加图' : '框选加图'}
          </button>
        )}
        <button className="ghost" onClick={() => setPageNo((n) => Math.max(1, n - 1))} disabled={pageNo <= 1}>
          ← 上一页
        </button>
        <span className="pageno">
          {pageNo} / {pageCount || '…'}
        </span>
        <button
          className="ghost"
          onClick={() => setPageNo((n) => Math.min(pageCount || n, n + 1))}
          disabled={!!pageCount && pageNo >= pageCount}
        >
          下一页 →
        </button>
        <button className="ghost" onClick={onClose}>
          关闭 ✕
        </button>
      </div>

      {addMode && (
        <div className={`addform ${pendingAdd ? 'ready' : ''}`}>
          <span className="addform-hint">
            {pendingAdd
              ? `已选 ${Math.round(pendingAdd[2] - pendingAdd[0])}×${Math.round(pendingAdd[3] - pendingAdd[1])}pt（p.${pageNo}）`
              : '在页面上拖出一个框'}
          </span>
          <input
            className="addform-input"
            value={addLabel}
            onChange={(e) => setAddLabel(e.target.value)}
            placeholder="图号（可留空）例如 图 4-50a"
            spellCheck={false}
            maxLength={200}
          />
          <input
            className="addform-input wide"
            value={addCaption}
            onChange={(e) => setAddCaption(e.target.value)}
            placeholder="图注（可留空）"
            spellCheck={false}
            maxLength={2000}
          />
          <button className="primary" disabled={!pendingAdd || saving} onClick={() => void saveAdded()}>
            {saving ? '添加中…' : '添加这张图'}
          </button>
        </div>
      )}

      <div className="viewer-stage" ref={wrapRef}>
        {status === 'loading' && <div className="viewer-msg">加载 PDF…</div>}
        {status === 'error' && (
          <div className="viewer-msg error">
            {error}
            <button className="ghost" onClick={retry} style={{ marginLeft: 12 }}>
              重试
            </button>
          </div>
        )}
        <div
          className={`page-wrap ${saving ? 'saving' : ''}`}
          style={{ display: status === 'ready' ? 'inline-block' : 'none' }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={(e) => void onPointerUp(e)}
          onPointerCancel={() => setDrag(null)}
        >
          <canvas ref={canvasRef} />
          {box && !addMode && (
            <div className="bbox" style={box}>
              <span className="bbox-tag">{label ?? 'figure'}</span>
            </div>
          )}
          {pendingAdd && (
            <div
              className="bbox add"
              style={{
                left: pendingAdd[0] * scale,
                top: pendingAdd[1] * scale,
                width: Math.max((pendingAdd[2] - pendingAdd[0]) * scale, 2),
                height: Math.max((pendingAdd[3] - pendingAdd[1]) * scale, 2),
              }}
            >
              <span className="bbox-tag">新图</span>
            </div>
          )}
          {drag && (
            <div
              className="bbox-draft"
              style={{
                left: Math.min(drag.x0, drag.x1),
                top: Math.min(drag.y0, drag.y1),
                width: Math.abs(drag.x1 - drag.x0),
                height: Math.abs(drag.y1 - drag.y0),
              }}
            />
          )}
        </div>
      </div>
    </div>
  )
}
