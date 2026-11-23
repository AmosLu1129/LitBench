// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 文献详情的「表格」页签（P0：表格进界面，2026-11-15）。
 *
 * 为什么单独一页：图早就是一等公民，表一直只在侧栏显示「N 表」。
 * `/api/papers/{id}/tables` 从 M1 起就能返回 Docling 抽出的结构化 HTML，
 * 这里把它变成可读、可定位、可引用的对象：
 *
 * - HTML 渲染：只保留表格结构与 colspan/rowspan，PDF 里抽出的标签/脚本一律丢掉；
 * - 点回原文：复用 PDF 查看器打开对应页（只读，不画框）；
 * - 复制引用：生成与学位论文交叉检查一致的 `\ref{tab:表号}`；
 * - 写进卡片：打开写作台并预选当前文献，表格会出现在 claim 证据列表里。
 *
 * 表格来自 Docling 增强档；pdfium 轻量档抽不到表结构，空数组不是错误。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { copyText, paperTables, tableLatex } from './api'
import type { PaperTable } from './types'

interface Props {
  paperId: string
  paperTitle: string
  onOpenSource: (table: PaperTable) => void
  onUseInWriting: () => void
  onToast: (message: string) => void
}

// 只允许表格结构元素；docling 偶发会带 <p>/<span> 等内联包装，一并保留并降级为文本
const ALLOWED_TAGS = new Set([
  'TABLE',
  'THEAD',
  'TBODY',
  'TFOOT',
  'TR',
  'TD',
  'TH',
  'CAPTION',
  'COLGROUP',
  'COL',
  'P',
  'SPAN',
  'BR',
  'STRONG',
  'B',
  'EM',
  'I',
  'SUP',
  'SUB',
])
// 允许的属性：合并单元格与 scope 是表格语义，class 只用于样式；其余（含 on*）全部剥掉
const ALLOWED_ATTRS = new Set(['colspan', 'rowspan', 'scope', 'class'])

/** 清洗后端返回的表格 HTML：白名单标签/属性，防 PDF 文本里的标签注入。 */
function sanitizeTableHtml(html: string): string {
  const template = document.createElement('template')
  template.innerHTML = html
  const clean = (parent: Node) => {
    for (const child of Array.from(parent.childNodes)) {
      if (child.nodeType === Node.COMMENT_NODE) {
        child.remove()
        continue
      }
      if (child.nodeType !== Node.ELEMENT_NODE) continue
      const element = child as HTMLElement
      if (!ALLOWED_TAGS.has(element.tagName)) {
        // 不在白名单：拆掉外壳保留文字（例如 <div>、<font>）
        element.replaceWith(...Array.from(element.childNodes))
        continue
      }
      for (const attr of Array.from(element.attributes)) {
        if (!ALLOWED_ATTRS.has(attr.name.toLowerCase())) element.removeAttribute(attr.name)
      }
      clean(element)
    }
  }
  clean(template.content)
  const root = template.content.firstElementChild
  // 极少数历史数据只有 <tr>；包一层 table 才能正常渲染
  if (root && root.tagName !== 'TABLE' && root.tagName !== 'COLGROUP') {
    const wrapper = document.createElement('table')
    wrapper.appendChild(template.content)
    return wrapper.outerHTML
  }
  return template.innerHTML
}

function tableRef(table: PaperTable): string {
  const label = (table.label_norm || table.label_raw || '').trim()
  return `\\ref{tab:${label || table.table_id}}`
}

export default function PaperTables({
  paperId,
  paperTitle,
  onOpenSource,
  onUseInWriting,
  onToast,
}: Props) {
  const [tables, setTables] = useState<PaperTable[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [latexBusy, setLatexBusy] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setTables(await paperTables(paperId))
    } catch (err) {
      setTables([])
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [paperId])

  useEffect(() => {
    setTables(null)
    void load()
  }, [load])

  const rows = useMemo(
    () =>
      (tables ?? []).map((table) => ({
        ...table,
        // 每张表的 HTML 只清洗一次，滚动/重渲染不重复解析
        safeHtml: table.html ? sanitizeTableHtml(table.html) : null,
      })),
    [tables],
  )

  const copyRef = (table: PaperTable) => {
    const text = tableRef(table)
    void copyText(text).then(() => onToast(`已复制 ${text}`))
  }

  const copyTableLatex = async (table: PaperTable) => {
    setLatexBusy(table.table_id)
    try {
      const payload = await tableLatex(paperId, table.table_id)
      await copyText(payload.code)
      const wrapNote = payload.wrapped ? '，已按页宽自动换行' : ''
      const note = payload.warnings.length > 0 ? `；${payload.warnings.join('；')}` : ''
      onToast(
        `已复制 LaTeX（${payload.columns} 列 × ${payload.rows} 行${wrapNote}${note}）`,
      )
    } catch (err) {
      onToast(err instanceof Error ? err.message : String(err))
    } finally {
      setLatexBusy(null)
    }
  }

  return (
    <div className="table-browser">
      <header className="table-browser-head">
        <div>
          <h3>
            表格
            {tables !== null && <span className="muted"> · {tables.length} 张</span>}
          </h3>
          <p className="muted">
            只显示最新版本、由 Docling 增强档抽出的结构化表；pdfium 轻量档抽不到表结构。
          </p>
        </div>
        <div className="table-browser-actions">
          <button className="ghost small" onClick={() => void load()} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </button>
          <button className="ghost small" onClick={onUseInWriting} title="打开写作台并把表格作为 claim 证据候选">
            写进卡片
          </button>
        </div>
      </header>

      {error && (
        <p className="table-error">
          表格载入失败：{error}
          <button className="link" onClick={() => void load()}>
            重试
          </button>
        </p>
      )}

      {tables === null ? (
        <p className="muted">载入表格…</p>
      ) : rows.length === 0 ? (
        <div className="empty table-empty">
          <h3>这篇文献还没有结构化表格</h3>
          <p>
            默认的 pdfium 轻量档只抽版面块，识别不出合并单元格与列头；
            需要表格时给这篇文献跑一次「按需升级 Docling」，重解析后再回到这里。
          </p>
        </div>
      ) : (
        <div className="table-cards">
          {rows.map((table) => (
            <article className="table-card" key={table.table_id}>
              <header className="table-card-head">
                <strong className="table-label">
                  {table.label_raw || table.label_norm || '表（未编号）'}
                </strong>
                <span className="table-chip">第 {table.page} 页</span>
                <span className={`table-chip ${table.quality === 'image' ? 'warn' : ''}`}>
                  {table.quality === 'image' ? '图片表' : '结构化'}
                </span>
                <span className="spacer" />
                <button
                  className="ghost small"
                  disabled={latexBusy === table.table_id}
                  onClick={() => void copyTableLatex(table)}
                  title="复制带合并单元格的 LaTeX table（需要 multirow 宏包时会提示）"
                >
                  {latexBusy === table.table_id ? '转换中…' : '复制 LaTeX'}
                </button>
                <button className="ghost small" onClick={() => copyRef(table)} title={tableRef(table)}>
                  复制引用
                </button>
                <button
                  className="ghost small"
                  onClick={() => onOpenSource(table)}
                  title="在原文 PDF 里定位到这张表"
                >
                  原文
                </button>
              </header>
              {table.caption && <p className="table-caption">{table.caption}</p>}
              {table.safeHtml ? (
                <div
                  className="table-html"
                  role="region"
                  aria-label={`${paperTitle} 第 ${table.page} 页表格`}
                  dangerouslySetInnerHTML={{ __html: table.safeHtml }}
                />
              ) : (
                <p className="table-nohtml">
                  这张表只保留了图片（没有单元格结构），可点「原文」回 PDF 查看。
                </p>
              )}
            </article>
          ))}
        </div>
      )}
    </div>
  )
}
