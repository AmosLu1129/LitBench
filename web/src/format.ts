// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 时间 / 文本小工具（修复批 D）。
 *
 * 后端批 E 起时间统一返回带时区的 ISO（如 `2026-11-13T00:57:15+00:00`）。
 * 以前各处用 `iso.replace('T', ' ').slice(0, 16)` 直接截字符串：拿到 `+00:00`
 * 会多出一截，而且显示的是 UTC 而不是用户本地时间。这里统一用 Date + Intl。
 */

const MINUTE_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const SECOND_FORMATTER = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

function formatDateTime(iso: string | null | undefined, withSeconds: boolean): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) {
    // 不是可解析的 ISO（老数据 / 自由文本）：原样返回，至少不丢信息
    return iso
  }
  return withSeconds ? SECOND_FORMATTER.format(date) : MINUTE_FORMATTER.format(date)
}

/** ISO 时间 → 本地时区「YYYY/MM/DD HH:mm」；空值显示「—」。 */
export function formatTime(iso: string | null | undefined): string {
  return formatDateTime(iso, false)
}

/** 同上，精确到秒（设置页「更新时间」用）。 */
export function formatTimeWithSeconds(iso: string | null | undefined): string {
  return formatDateTime(iso, true)
}

/** 要点编辑框按行拆：每行一条，最多 limit 条（超出部分由调用方截断并提示）。 */
export function splitKeyPoints(
  raw: string,
  limit = 8,
): { points: string[]; total: number; truncated: number } {
  const lines = raw
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  return {
    points: lines.slice(0, limit),
    total: lines.length,
    truncated: Math.max(0, lines.length - limit),
  }
}
