// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 程序化贴图：M6a 阶段不依赖任何外部素材，全部用 canvas 现画。
 * 目的不是最终质感，而是先让构图、光位、色调可被判断。
 */
import * as THREE from 'three'

function pick(seed: number): () => number {
  let s = seed >>> 0
  return () => {
    s = (s + 0x6d2b79f5) >>> 0
    let t = Math.imul(s ^ (s >>> 15), 1 | s)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function ctx2d(w: number, h: number): CanvasRenderingContext2D {
  const c = document.createElement('canvas')
  c.width = w
  c.height = h
  const ctx = c.getContext('2d')
  if (!ctx) throw new Error('2d context 不可用')
  return ctx
}

function tex(canvas: HTMLCanvasElement, srgb = true): THREE.CanvasTexture {
  const t = new THREE.CanvasTexture(canvas)
  if (srgb) t.colorSpace = THREE.SRGBColorSpace
  t.anisotropy = 8
  t.needsUpdate = true
  return t
}

/** 墙上的回弹光：中心透空，只让窗口四周的墙面浮出一层暖 */
export function makeWallGlowTexture(): THREE.CanvasTexture {
  const w = 512
  const h = 512
  const ctx = ctx2d(w, h)
  ctx.clearRect(0, 0, w, h)
  const g = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w / 2)
  g.addColorStop(0, 'rgba(255,201,138,0)')
  g.addColorStop(0.33, 'rgba(255,201,138,0)')
  g.addColorStop(0.5, 'rgba(255,201,138,0.42)')
  g.addColorStop(0.78, 'rgba(255,201,138,0.1)')
  g.addColorStop(1, 'rgba(255,201,138,0)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)
  return tex(ctx.canvas)
}

/** 宣纸底：米白 + 噪点 + 帘纹 + 边缘微暗 */
function paintPaper(ctx: CanvasRenderingContext2D, w: number, h: number) {
  ctx.fillStyle = '#efe3c9'
  ctx.fillRect(0, 0, w, h)

  const r = pick(7)
  for (let i = 0; i < 9000; i++) {
    const x = r() * w
    const y = r() * h
    const a = 0.015 + r() * 0.05
    ctx.fillStyle = r() > 0.5 ? `rgba(120,100,70,${a})` : `rgba(255,250,236,${a})`
    ctx.fillRect(x, y, 1 + r() * 2, 1 + r() * 2)
  }
  // 帘纹：细横线，间距随纸张走
  ctx.strokeStyle = 'rgba(120,100,70,0.045)'
  ctx.lineWidth = 1
  for (let y = 0; y < h; y += 7) {
    ctx.beginPath()
    ctx.moveTo(0, y + 0.5)
    ctx.lineTo(w, y + 0.5)
    ctx.stroke()
  }
  // 边缘微暗（老纸的自然衰减）
  const g = ctx.createRadialGradient(w / 2, h / 2, Math.min(w, h) * 0.25, w / 2, h / 2, Math.max(w, h) * 0.72)
  g.addColorStop(0, 'rgba(90,70,45,0)')
  g.addColorStop(1, 'rgba(90,70,45,0.16)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)
}

export function makePaperTexture(): THREE.CanvasTexture {
  const w = 1024
  const h = 1024
  const ctx = ctx2d(w, h)
  paintPaper(ctx, w, h)
  return tex(ctx.canvas)
}

/** 室内墙面：深赭灰泥，带大尺度斑驳，暗处也能读出"这是一面墙" */
export function makePlasterTexture(): THREE.CanvasTexture {
  const w = 1024
  const h = 1024
  const ctx = ctx2d(w, h)
  ctx.fillStyle = '#3a2f26'
  ctx.fillRect(0, 0, w, h)
  const r = pick(23)
  for (let i = 0; i < 220; i++) {
    const cx = r() * w
    const cy = r() * h
    const rad = 30 + r() * 150
    const a = 0.02 + r() * 0.05
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, rad)
    g.addColorStop(0, r() > 0.5 ? `rgba(90,74,58,${a})` : `rgba(26,20,16,${a})`)
    g.addColorStop(1, 'rgba(0,0,0,0)')
    ctx.fillStyle = g
    ctx.beginPath()
    ctx.arc(cx, cy, rad, 0, Math.PI * 2)
    ctx.fill()
  }
  for (let i = 0; i < 12000; i++) {
    const a = 0.01 + r() * 0.03
    ctx.fillStyle = r() > 0.5 ? `rgba(120,100,80,${a})` : `rgba(20,15,11,${a})`
    ctx.fillRect(r() * w, r() * h, 1 + r() * 2, 1 + r() * 2)
  }
  const g2 = ctx.createLinearGradient(0, 0, 0, h)
  g2.addColorStop(0, 'rgba(10,8,6,0.28)')
  g2.addColorStop(0.55, 'rgba(10,8,6,0)')
  g2.addColorStop(1, 'rgba(10,8,6,0.35)')
  ctx.fillStyle = g2
  ctx.fillRect(0, 0, w, h)
  return tex(ctx.canvas)
}

/** 远山剪影（宋画平远：一层层淡墨，脊线亮、山脚沉） */
export function makeMountainTexture(color: string, seed: number): THREE.CanvasTexture {
  const w = 1024
  const h = 256
  const ctx = ctx2d(w, h)
  ctx.clearRect(0, 0, w, h)
  const r = pick(seed)
  const ridge: number[] = []
  let y = h * (0.5 + r() * 0.15)
  for (let x = 0; x <= w; x += 14) {
    y += (r() - 0.5) * 14
    y = Math.max(h * 0.22, Math.min(h * 0.78, y))
    ridge.push(y)
  }
  const grad = ctx.createLinearGradient(0, h * 0.2, 0, h)
  grad.addColorStop(0, color)
  grad.addColorStop(0.55, color)
  grad.addColorStop(1, 'rgba(18,26,26,0.9)')
  ctx.beginPath()
  ctx.moveTo(0, h)
  ridge.forEach((yy, i) => ctx.lineTo(i * 14, yy))
  ctx.lineTo(w, h)
  ctx.closePath()
  ctx.fillStyle = grad
  ctx.fill()
  return tex(ctx.canvas)
}

/** 木纹 */export function makeWoodTexture(): THREE.CanvasTexture {
  const w = 1024
  const h = 1024
  const ctx = ctx2d(w, h)
  const r = pick(19)
  ctx.fillStyle = '#4a3628'
  ctx.fillRect(0, 0, w, h)
  for (let i = 0; i < 260; i++) {
    const y = r() * h
    const a = 0.03 + r() * 0.1
    ctx.strokeStyle = r() > 0.5 ? `rgba(24,16,12,${a})` : `rgba(120,90,64,${a})`
    ctx.lineWidth = 1 + r() * 3
    ctx.beginPath()
    ctx.moveTo(0, y)
    const amp = 4 + r() * 16
    for (let x = 0; x <= w; x += 32) {
      ctx.lineTo(x, y + Math.sin((x / w) * Math.PI * (1 + r() * 0.02) * 6 + i) * amp * 0.15)
    }
    ctx.stroke()
  }
  // 木结
  for (let k = 0; k < 5; k++) {
    const cx = r() * w
    const cy = r() * h
    for (let i = 6; i > 0; i--) {
      ctx.strokeStyle = `rgba(20,13,9,${0.05 + i * 0.02})`
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.ellipse(cx, cy, i * 9, i * 4.5, 0.3, 0, Math.PI * 2)
      ctx.stroke()
    }
  }
  return tex(ctx.canvas)
}

/** 书卷：竖排墨字 + 朱批 + 引线 + 插图 + 朱印 */
export function makeScrollTexture(): THREE.CanvasTexture {
  const w = 2048
  const h = 740
  const ctx = ctx2d(w, h)
  paintPaper(ctx, w, h)

  const r = pick(31)
  const ink = 'rgba(26,21,17,0.82)'
  ctx.strokeStyle = ink
  ctx.fillStyle = ink

  // 竖排文字：右起为文，左端为图
  const colW = 62
  const glyph = 40
  const top = 88
  const cols = 20
  const startX = w - 140
  for (let c = 0; c < cols; c++) {
    const x = startX - c * colW
    if (x < 720) break
    const n = 12 + Math.floor(r() * 4)
    for (let i = 0; i < n; i++) {
      const y = top + i * (glyph + 18)
      if (y + glyph > h - 90) break
      const strokes = 5 + Math.floor(r() * 4)
      for (let s = 0; s < strokes; s++) {
        const sx = x + 5 + r() * (glyph - 10)
        const sy = y + 5 + r() * (glyph - 10)
        const len = 10 + r() * 20
        const vert = r() > 0.42
        ctx.lineWidth = 2.8 + r() * 1.4
        ctx.beginPath()
        ctx.moveTo(sx, sy)
        if (vert) ctx.lineTo(sx + (r() - 0.5) * 3, sy + len)
        else ctx.lineTo(sx + len, sy + (r() - 0.5) * 3)
        ctx.stroke()
      }
    }
  }

  // 界行：细竖线，古籍的版式感
  ctx.strokeStyle = 'rgba(110,88,62,0.16)'
  ctx.lineWidth = 1.4
  for (let c = 0; c <= cols; c++) {
    const x = startX + colW * 0.5 - c * colW
    if (x < 700) break
    ctx.beginPath()
    ctx.moveTo(x, 70)
    ctx.lineTo(x, h - 70)
    ctx.stroke()
  }

  // 朱批：两处竖批 + 圈点
  ctx.strokeStyle = 'rgba(166,59,43,0.85)'
  ctx.lineWidth = 2.4
  for (const [x, y0, y1] of [
    [w - 150 - 4 * colW - 20, 180, 430],
    [w - 150 - 9 * colW - 20, 250, 520],
  ] as const) {
    ctx.beginPath()
    ctx.moveTo(x, y0)
    ctx.lineTo(x + 2, y1)
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(x + 3, y1 + 16, 6, 0, Math.PI * 2)
    ctx.stroke()
  }

  // 插图框：墨线坐标 + 折线（像论文里的曲线图）
  const fx = 150
  const fy = 120
  const fw = 520
  const fh = 380
  ctx.strokeStyle = 'rgba(26,21,17,0.6)'
  ctx.lineWidth = 2
  ctx.strokeRect(fx, fy, fw, fh)
  ctx.beginPath()
  ctx.moveTo(fx + 40, fy + fh - 40)
  ctx.lineTo(fx + 40, fy + 30)
  ctx.moveTo(fx + 40, fy + fh - 40)
  ctx.lineTo(fx + fw - 25, fy + fh - 40)
  ctx.stroke()
  ctx.strokeStyle = 'rgba(26,21,17,0.8)'
  ctx.lineWidth = 2.6
  ctx.beginPath()
  for (let i = 0; i <= 24; i++) {
    const px = fx + 40 + (i / 24) * (fw - 70)
    const py = fy + fh - 60 - Math.sin(i * 0.42) * 60 - (i / 24) * 90 + (r() - 0.5) * 12
    if (i === 0) ctx.moveTo(px, py)
    else ctx.lineTo(px, py)
  }
  ctx.stroke()
  // 图注：一行小字
  ctx.fillStyle = ink
  for (let i = 0; i < 12; i++) {
    ctx.fillRect(fx + 40 + i * 30, fy + fh + 26, 18, 4)
  }

  // 引线：从正文某一列连到插图（朱红细线，示意“出处”）
  ctx.strokeStyle = 'rgba(166,59,43,0.9)'
  ctx.lineWidth = 3
  ctx.beginPath()
  ctx.moveTo(w - 150 - 12 * colW + 20, 330)
  ctx.bezierCurveTo(w - 150 - 12 * colW - 60, 300, fx + fw + 90, 360, fx + fw + 8, 372)
  ctx.stroke()

  // 引线落点的小框（bbox 的那个框）
  ctx.strokeStyle = 'rgba(166,59,43,0.95)'
  ctx.lineWidth = 3
  ctx.strokeRect(fx + 210, fy + 120, 120, 90)

  // 朱印
  ctx.fillStyle = 'rgba(166,59,43,0.92)'
  ctx.fillRect(w - 210, h - 200, 92, 92)
  ctx.fillStyle = 'rgba(243,230,210,0.95)'
  ctx.fillRect(w - 204, h - 194, 80, 80)
  ctx.strokeStyle = 'rgba(243,230,210,0.95)'
  ctx.lineWidth = 5
  ctx.beginPath()
  ctx.moveTo(w - 168, h - 186)
  ctx.lineTo(w - 168, h - 122)
  ctx.moveTo(w - 168, h - 176)
  ctx.lineTo(w - 186, h - 176)
  ctx.moveTo(w - 168, h - 152)
  ctx.lineTo(w - 186, h - 152)
  ctx.moveTo(w - 168, h - 128)
  ctx.lineTo(w - 186, h - 128)
  ctx.stroke()

  return tex(ctx.canvas)
}

/** 窗外天色：靛青到浅金，薄云 */
export function makeSkyTexture(): THREE.CanvasTexture {
  const w = 1024
  const h = 512
  const ctx = ctx2d(w, h)
  const g = ctx.createLinearGradient(0, 0, 0, h)
  g.addColorStop(0, '#1e2a41')
  g.addColorStop(0.42, '#4a5a6e')
  g.addColorStop(0.68, '#a8927a')
  g.addColorStop(0.84, '#e6bd86')
  g.addColorStop(1, '#f6d9a4')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)

  const r = pick(53)
  for (let i = 0; i < 26; i++) {
    const cx = r() * w
    const cy = h * (0.25 + r() * 0.45)
    const rx = 80 + r() * 220
    const ry = 12 + r() * 26
    const a = 0.03 + r() * 0.06
    const cg = ctx.createRadialGradient(cx, cy, 0, cx, cy, rx)
    cg.addColorStop(0, `rgba(255,236,209,${a})`)
    cg.addColorStop(1, 'rgba(255,236,209,0)')
    ctx.fillStyle = cg
    ctx.beginPath()
    ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2)
    ctx.fill()
  }
  return tex(ctx.canvas)
}

/** 桃枝：折枝一枝斜入，带花与苞 */
export function makeBranchTexture(): THREE.CanvasTexture {
  const w = 1024
  const h = 640
  const ctx = ctx2d(w, h)
  ctx.clearRect(0, 0, w, h)
  const r = pick(71)

  const branch = (x0: number, y0: number, x1: number, y1: number, cx: number, cy: number, width: number) => {
    ctx.strokeStyle = '#2f231b'
    ctx.lineCap = 'round'
    ctx.lineWidth = width
    ctx.beginPath()
    ctx.moveTo(x0, y0)
    ctx.quadraticCurveTo(cx, cy, x1, y1)
    ctx.stroke()
  }

  branch(1010, 620, 560, 210, 860, 520, 22)
  branch(860, 470, 640, 120, 800, 300, 12)
  branch(780, 380, 520, 330, 660, 330, 9)
  branch(900, 300, 1010, 90, 980, 190, 8)
  branch(700, 250, 610, 60, 640, 150, 7)

  const blossom = (x: number, y: number, s: number) => {
    ctx.fillStyle = `rgba(232,183,174,${0.75 + r() * 0.2})`
    for (let p = 0; p < 5; p++) {
      const a = (p / 5) * Math.PI * 2 + r() * 0.3
      ctx.beginPath()
      ctx.ellipse(x + Math.cos(a) * s, y + Math.sin(a) * s, s * 0.95, s * 0.72, a, 0, Math.PI * 2)
      ctx.fill()
    }
    ctx.fillStyle = 'rgba(150,72,62,0.9)'
    ctx.beginPath()
    ctx.arc(x, y, s * 0.34, 0, Math.PI * 2)
    ctx.fill()
  }

  for (let i = 0; i < 34; i++) {
    // 花沿着几条枝分布
    const t = r()
    const x = 1010 - t * 460 + (r() - 0.5) * 120
    const y = 600 - t * 420 + (r() - 0.5) * 120
    blossom(x, y, 9 + r() * 10)
  }
  for (const [x, y] of [
    [640, 120],
    [1010, 90],
    [520, 330],
  ] as const) {
    blossom(x, y, 12)
  }
  return tex(ctx.canvas)
}

/** 飞鸟剪影 */
export function makeBirdTexture(up: boolean): THREE.CanvasTexture {
  const w = 256
  const h = 256
  const ctx = ctx2d(w, h)
  ctx.clearRect(0, 0, w, h)
  ctx.fillStyle = '#181410'
  ctx.strokeStyle = '#181410'
  ctx.lineWidth = 9
  ctx.lineCap = 'round'
  ctx.beginPath()
  ctx.moveTo(18, up ? 150 : 100)
  ctx.quadraticCurveTo(88, up ? 40 : 170, 128, 128)
  ctx.quadraticCurveTo(170, up ? 40 : 170, 238, up ? 150 : 100)
  ctx.stroke()
  ctx.beginPath()
  ctx.ellipse(128, 132, 13, 8, 0, 0, Math.PI * 2)
  ctx.fill()
  return tex(ctx.canvas)
}

/** 灯焰光晕 sprite */
export function makeGlowTexture(): THREE.CanvasTexture {
  const w = 256
  const h = 256
  const ctx = ctx2d(w, h)
  const g = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w / 2)
  g.addColorStop(0, 'rgba(255,214,150,0.95)')
  g.addColorStop(0.25, 'rgba(255,168,88,0.45)')
  g.addColorStop(0.6, 'rgba(255,140,60,0.12)')
  g.addColorStop(1, 'rgba(255,120,40,0)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)
  return tex(ctx.canvas)
}

/** 尘埃点 */
export function makeDustTexture(): THREE.CanvasTexture {
  const w = 64
  const h = 64
  const ctx = ctx2d(w, h)
  const g = ctx.createRadialGradient(32, 32, 0, 32, 32, 32)
  g.addColorStop(0, 'rgba(255,236,206,1)')
  g.addColorStop(0.4, 'rgba(255,224,180,0.5)')
  g.addColorStop(1, 'rgba(255,210,160,0)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)
  return tex(ctx.canvas)
}

/** 窗外薄雾带 */
export function makeMistTexture(): THREE.CanvasTexture {
  const w = 512
  const h = 256
  const ctx = ctx2d(w, h)
  const g = ctx.createLinearGradient(0, 0, 0, h)
  g.addColorStop(0, 'rgba(255,240,214,0)')
  g.addColorStop(0.55, 'rgba(255,238,210,0.55)')
  g.addColorStop(1, 'rgba(255,236,208,0)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)
  return tex(ctx.canvas)
}

/** 接触阴影（物体落在桌面上的暗影） */
export function makeContactShadowTexture(): THREE.CanvasTexture {
  const s = 256
  const ctx = ctx2d(s, s)
  ctx.clearRect(0, 0, s, s)
  const g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2)
  g.addColorStop(0, 'rgba(0,0,0,0.62)')
  g.addColorStop(0.42, 'rgba(0,0,0,0.34)')
  g.addColorStop(0.78, 'rgba(0,0,0,0.08)')
  g.addColorStop(1, 'rgba(0,0,0,0)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, s, s)
  return tex(ctx.canvas, false)
}

/** 微表面起伏（bumpMap 用灰阶）：paper / wood / plaster */
export function makeBumpTexture(kind: 'paper' | 'wood' | 'plaster', seed = 5): THREE.CanvasTexture {
  const w = 512
  const h = 512
  const ctx = ctx2d(w, h)
  const r = pick(seed)
  ctx.fillStyle = '#808080'
  ctx.fillRect(0, 0, w, h)
  if (kind === 'paper') {
    for (let i = 0; i < 26000; i++) {
      const v = 118 + Math.floor(r() * 24)
      ctx.fillStyle = `rgb(${v},${v},${v})`
      ctx.fillRect(r() * w, r() * h, 1 + r() * 2, 1 + r() * 2)
    }
    ctx.strokeStyle = 'rgba(96,96,96,0.35)'
    for (let y = 0; y < h; y += 6) {
      ctx.beginPath()
      ctx.moveTo(0, y)
      ctx.lineTo(w, y)
      ctx.stroke()
    }
  } else if (kind === 'wood') {
    for (let i = 0; i < 240; i++) {
      const y = r() * h
      ctx.strokeStyle = r() > 0.5 ? 'rgba(150,150,150,0.5)' : 'rgba(96,96,96,0.5)'
      ctx.lineWidth = 1 + r() * 4
      ctx.beginPath()
      ctx.moveTo(0, y)
      for (let x = 0; x <= w; x += 24) ctx.lineTo(x, y + Math.sin(x * 0.02 + i) * 4)
      ctx.stroke()
    }
  } else {
    for (let i = 0; i < 2200; i++) {
      const v = 110 + Math.floor(r() * 36)
      ctx.fillStyle = `rgba(${v},${v},${v},0.5)`
      const rad = 2 + r() * 12
      ctx.beginPath()
      ctx.arc(r() * w, r() * h, rad, 0, Math.PI * 2)
      ctx.fill()
    }
  }
  return tex(ctx.canvas, false)
}

/** 环境反射：暗室 + 一扇亮窗 + 一点暖灯，给金属/瓷器一点"住过人"的反光 */
export function makeEnvEquirect(): THREE.CanvasTexture {
  const w = 1024
  const h = 512
  const ctx = ctx2d(w, h)
  const g = ctx.createLinearGradient(0, 0, 0, h)
  g.addColorStop(0, '#0b0908')
  g.addColorStop(0.45, '#191310')
  g.addColorStop(0.72, '#241a14')
  g.addColorStop(1, '#100c09')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)

  // 窗（右侧一块冷亮的矩形）
  const wg = ctx.createLinearGradient(0, h * 0.32, 0, h * 0.62)
  wg.addColorStop(0, 'rgba(126,150,178,0.85)')
  wg.addColorStop(0.55, 'rgba(224,196,150,0.95)')
  wg.addColorStop(1, 'rgba(255,214,150,0.9)')
  ctx.fillStyle = wg
  ctx.fillRect(w * 0.55, h * 0.3, w * 0.18, h * 0.34)

  // 灯（左侧暖色云）
  const lg = ctx.createRadialGradient(w * 0.16, h * 0.52, 0, w * 0.16, h * 0.52, w * 0.12)
  lg.addColorStop(0, 'rgba(255,176,96,1)')
  lg.addColorStop(0.35, 'rgba(255,140,60,0.45)')
  lg.addColorStop(1, 'rgba(255,120,40,0)')
  ctx.fillStyle = lg
  ctx.fillRect(0, 0, w, h)
  const t = tex(ctx.canvas)
  t.mapping = THREE.EquirectangularReflectionMapping
  return t
}

/** 桃花瓣（单瓣，alpha） */
export function makeBlossomTexture(): THREE.CanvasTexture {
  const s = 128
  const ctx = ctx2d(s, s)
  ctx.clearRect(0, 0, s, s)
  const g = ctx.createRadialGradient(s / 2, s / 2, 2, s / 2, s / 2, s / 2)
  g.addColorStop(0, 'rgba(255,236,232,1)')
  g.addColorStop(0.62, 'rgba(240,196,188,1)')
  g.addColorStop(1, 'rgba(214,150,142,0)')
  ctx.fillStyle = g
  for (let i = 0; i < 5; i++) {
    const a = (i / 5) * Math.PI * 2 - Math.PI / 2
    ctx.beginPath()
    ctx.ellipse(s / 2 + Math.cos(a) * 16, s / 2 + Math.sin(a) * 16, 17, 13, a, 0, Math.PI * 2)
    ctx.fill()
  }
  ctx.fillStyle = 'rgba(168,88,74,0.9)'
  ctx.beginPath()
  ctx.arc(s / 2, s / 2, 9, 0, Math.PI * 2)
  ctx.fill()
  return tex(ctx.canvas)
}

/** 花瓣/叶片单瓣（飘落用） */
export function makePetalTexture(): THREE.CanvasTexture {
  const s = 64
  const ctx = ctx2d(s, s)
  ctx.clearRect(0, 0, s, s)
  const g = ctx.createRadialGradient(s / 2, s / 2, 1, s / 2, s / 2, s / 2)
  g.addColorStop(0, 'rgba(255,240,236,1)')
  g.addColorStop(0.55, 'rgba(240,198,190,1)')
  g.addColorStop(1, 'rgba(214,150,142,0)')
  ctx.fillStyle = g
  ctx.beginPath()
  ctx.ellipse(s / 2, s / 2, 26, 16, 0.5, 0, Math.PI * 2)
  ctx.fill()
  return tex(ctx.canvas)
}

