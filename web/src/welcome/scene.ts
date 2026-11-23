// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 欢迎页场景控制器（M6b）：渲染器 + 相机轨道 + 34 秒时间轴 + 后处理链。
 * 只依赖 three / stage / post，不依赖 React。
 */
import * as THREE from 'three'
import { buildStage, type Stage } from './stage'
import { createPost, type Post } from './post'

export type WelcomeEvent = 'creak' | 'birds' | 'page'
export type WelcomePhase = 'intro' | 'inspect' | 'reveal'

export interface SceneOptions {
  startAt?: number
  frozen?: boolean
  onDone?: () => void
  onEvent?: (name: WelcomeEvent) => void
  onPhase?: (phase: WelcomePhase) => void
}

const DURATION = 34

type CamKey = {
  t: number
  pos: [number, number, number]
  look: [number, number, number]
  fov: number
}

// 一镜到底：起幅看桌面、开窗、推近书卷、抬眼看窗、回落定格
const CAM_KEYS: CamKey[] = [
  { t: 0, pos: [0.02, 0.78, 1.58], look: [0, 0.55, -1.05], fov: 48 },
  { t: 4, pos: [0.02, 0.77, 1.52], look: [0, 0.54, -1.05], fov: 48 },
  { t: 9, pos: [0.03, 0.72, 1.34], look: [0, 0.42, -0.9], fov: 46 },
  { t: 13, pos: [0.04, 0.6, 1.0], look: [0, 0.18, -0.4], fov: 42 },
  { t: 18, pos: [0.1, 0.4, 0.62], look: [-0.02, 0.1, -0.16], fov: 36 },
  { t: 22, pos: [0.06, 0.62, 0.92], look: [0.03, 1.02, -1.35], fov: 44 },
  { t: 26, pos: [0.02, 0.72, 1.12], look: [0, 0.52, -0.95], fov: 46 },
  { t: 34, pos: [0.02, 0.75, 1.2], look: [0, 0.55, -1.0], fov: 47 },
]

function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)))
  return t * t * (3 - 2 * t)
}

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t
}

/** 3–4s 风试探推 10°，停 1s，5–7.5s 全开 */
function openProgress(t: number): number {
  if (t <= 3) return 0
  if (t <= 4) return smoothstep(3, 4, t) * 0.18
  if (t <= 5) return 0.18
  return lerp(0.18, 1, smoothstep(5, 7.5, t))
}

function sampleCamera(t: number): { pos: THREE.Vector3; look: THREE.Vector3; fov: number } {
  let i = 0
  while (i < CAM_KEYS.length - 2 && t > CAM_KEYS[i + 1].t) i++
  const a = CAM_KEYS[i]
  const b = CAM_KEYS[i + 1]
  const u = smoothstep(0, 1, (t - a.t) / Math.max(0.0001, b.t - a.t))
  return {
    pos: new THREE.Vector3(lerp(a.pos[0], b.pos[0], u), lerp(a.pos[1], b.pos[1], u), lerp(a.pos[2], b.pos[2], u)),
    look: new THREE.Vector3(
      lerp(a.look[0], b.look[0], u),
      lerp(a.look[1], b.look[1], u),
      lerp(a.look[2], b.look[2], u),
    ),
    fov: lerp(a.fov, b.fov, u),
  }
}

/** 跟焦：起幅对窗，开窗后对书卷，特写对纸面，抬眼看窗外，收尾回桌面 */
const FOCUS_KEYS: [number, number][] = [
  [0, 2.75],
  [6, 2.6],
  [12, 1.9],
  [16, 1.0],
  [20, 0.95],
  [24, 2.3],
  [34, 2.35],
]

function focusAt(t: number): number {
  for (let i = 0; i < FOCUS_KEYS.length - 1; i++) {
    const [t0, f0] = FOCUS_KEYS[i]
    const [t1, f1] = FOCUS_KEYS[i + 1]
    if (t <= t1) return lerp(f0, f1, smoothstep(t0, t1, t))
  }
  return FOCUS_KEYS[FOCUS_KEYS.length - 1][1]
}

export class WelcomeScene {
  private container: HTMLElement
  private renderer: THREE.WebGLRenderer
  private scene = new THREE.Scene()
  private camera: THREE.PerspectiveCamera
  private stage: Stage
  private post: Post
  private envRT: THREE.WebGLRenderTarget
  private clock = new THREE.Clock()
  private raf = 0
  private time: number
  private frozen: boolean
  private duration = DURATION
  private opts: SceneOptions
  private done = false
  private fired = new Set<WelcomeEvent>()
  private phase: WelcomePhase = 'intro'
  private lastOpen = -1
  private mouse = new THREE.Vector2()
  private mouseSmooth = new THREE.Vector2()
  private ro: ResizeObserver
  private dirTmp = new THREE.Vector3()
  private onPointerMove = (e: PointerEvent) => {
    this.mouse.set((e.clientX / window.innerWidth) * 2 - 1, (e.clientY / window.innerHeight) * 2 - 1)
  }
  private onVisibility = () => {
    if (document.hidden) this.stop()
    else this.start()
  }

  constructor(container: HTMLElement, opts: SceneOptions = {}) {
    this.container = container
    this.opts = opts
    this.frozen = opts.frozen === true
    this.time = opts.startAt ?? 0

    this.renderer = new THREE.WebGLRenderer({ antialias: false, powerPreference: 'high-performance' })
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5))
    this.renderer.setSize(container.clientWidth || window.innerWidth, container.clientHeight || window.innerHeight)
    this.renderer.outputColorSpace = THREE.SRGBColorSpace
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping
    this.renderer.toneMappingExposure = 0.72
    this.renderer.shadowMap.enabled = true
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap
    // 场景基本静态：阴影贴图默认不重建，只有窗扇在动时才更新，
    // 避免每帧重算带来的边缘抖动（看起来就是"投影频闪"）
    this.renderer.shadowMap.autoUpdate = false
    this.renderer.shadowMap.needsUpdate = true
    this.renderer.domElement.className = 'welcome-canvas-el'
    container.appendChild(this.renderer.domElement)

    this.scene.background = new THREE.Color(0x0b0908)
    this.scene.fog = new THREE.FogExp2(0x120e0b, 0.16)

    this.camera = new THREE.PerspectiveCamera(46, this.aspect(), 0.04, 420)

    this.stage = buildStage()
    this.scene.add(this.stage.root)

    // 环境反射（金属、瓷器、漆器靠它才像真的）
    const pmrem = new THREE.PMREMGenerator(this.renderer)
    this.envRT = pmrem.fromEquirectangular(this.stage.envTexture)
    this.scene.environment = this.envRT.texture
    this.scene.environmentIntensity = 0.45
    pmrem.dispose()

    this.post = createPost(this.renderer, this.scene, this.camera)
    this.post.composer.setPixelRatio(this.renderer.getPixelRatio())
    this.post.setSize(this.width, this.height)

    window.addEventListener('pointermove', this.onPointerMove)
    document.addEventListener('visibilitychange', this.onVisibility)
    this.ro = new ResizeObserver(() => this.resize())
    this.ro.observe(container)

    this.applyTime(this.time)
    this.renderFrame()
    this.start()
  }

  private get width(): number {
    return this.container.clientWidth || window.innerWidth
  }

  private get height(): number {
    return this.container.clientHeight || window.innerHeight
  }

  private aspect(): number {
    return this.width / Math.max(1, this.height)
  }

  private resize() {
    this.renderer.setSize(this.width, this.height)
    this.camera.aspect = this.aspect()
    this.camera.updateProjectionMatrix()
    this.post.setSize(this.width, this.height)
  }

  private fire(name: WelcomeEvent, at: number) {
    if (this.time >= at && !this.fired.has(name)) {
      this.fired.add(name)
      this.opts.onEvent?.(name)
    }
  }

  private applyTime(t: number) {
    const st = this.stage
    const open = openProgress(t)

    for (const s of st.sashes) {
      s.pivot.rotation.y = s.dir * THREE.MathUtils.degToRad(78) * open
    }
    // 只有窗扇角度真的变了才重算阴影（开窗那 3 秒），其余时间冻结，投影不再抖
    if (Math.abs(open - this.lastOpen) > 0.0002) {
      this.lastOpen = open
      this.renderer.shadowMap.needsUpdate = true
    }

    // 黎明：窗外从暗到亮，太阳在窗外升起（开窗后刻意给亮）
    const daylight = 0.1 + 0.9 * open
    st.setDaylight(daylight)
    st.sun.intensity = 3.2 * smoothstep(0.04, 0.96, open) + 0.01
    st.sun.color.setRGB(1.0, 0.82, 0.62)
    st.coolFill.intensity = 0.26 * (1 - open * 0.6)
    st.wallGlowMat.opacity = 0.2 * smoothstep(0.1, 1, open)

    for (let i = 0; i < st.mistMats.length; i++) {
      st.mistMats[i].opacity = (0.07 + i * 0.05) * smoothstep(0.05, 0.85, open)
    }

    this.renderer.toneMappingExposure = lerp(0.72, 0.87, smoothstep(0, 1, Math.min(1, Math.max(0, (t - 4) / 9))))


    // 灯焰只做轻微呼吸（±3%），2Hz 级别的强闪会让人误以为投影在频闪
    const flicker = 1 + Math.sin(t * 3.1) * 0.018 + Math.sin(t * 5.3 + 1.7) * 0.012
    st.lamp.intensity = 2.2 * flicker
    st.dustMat.opacity = 0.04 + 0.16 * smoothstep(0.08, 1, open)

    // 相机
    const cam = sampleCamera(t)
    this.mouseSmooth.lerp(this.mouse, 0.06)
    this.camera.position.set(
      cam.pos.x + this.mouseSmooth.x * 0.035,
      cam.pos.y - this.mouseSmooth.y * 0.02,
      cam.pos.z,
    )
    this.camera.lookAt(cam.look)
    st.skyMat.uniforms.uSunDir.value.copy(st.sunPos).sub(this.camera.position).normalize()
    if (Math.abs(this.camera.fov - cam.fov) > 0.01) {
      this.camera.fov = cam.fov
      this.camera.updateProjectionMatrix()
    }

    // 景深：跟焦曲线（起幅对窗 → 书卷 → 纸面 → 窗外 → 桌面）
    this.camera.getWorldDirection(this.dirTmp)
    const focus = focusAt(t)
    this.post.update({
      time: t,
      sun: st.sunPos,
      sunIntensity: 0.28 * smoothstep(0.08, 0.9, open),
      focus,
      aperture: t > 14 && t < 21 ? 0.0028 : 0.0016,
    })

    // 火苗 / 烟 / 鸟 / 花瓣
    st.setTime(t)

    this.fire('creak', 3.0)
    this.fire('birds', 18.2)
    this.fire('page', 24.4)

    const phase: WelcomePhase = t < 13 ? 'intro' : t < 20 ? 'inspect' : 'reveal'
    if (phase !== this.phase) {
      this.phase = phase
      this.opts.onPhase?.(phase)
    }
  }

  private renderFrame() {
    this.post.composer.render()
  }

  private tick = () => {
    this.raf = requestAnimationFrame(this.tick)
    const dt = Math.min(0.05, this.clock.getDelta())
    if (!this.frozen) {
      this.time += dt
      if (this.time >= this.duration) {
        this.time = this.duration
        if (!this.done) {
          this.done = true
          this.opts.onDone?.()
        }
      }
    }
    this.dustDrift(dt)
    this.applyTime(this.time)
    this.renderFrame()
  }

  private dustDrift(dt: number) {
    const attr = this.stage.dust.geometry.getAttribute('position') as THREE.BufferAttribute
    const arr = attr.array as Float32Array
    const step = this.frozen ? 0 : dt
    for (let i = 0; i < arr.length; i += 3) {
      arr[i] += Math.sin(this.time * 0.5 + i) * 0.00002 + step * 0.004
      arr[i + 1] += step * 0.004
      if (arr[i + 1] > 1.35) arr[i + 1] = 0.08
    }
    attr.needsUpdate = true
  }

  start() {
    if (this.raf) return
    this.clock.getDelta()
    this.raf = requestAnimationFrame(this.tick)
  }

  stop() {
    if (this.raf) cancelAnimationFrame(this.raf)
    this.raf = 0
  }

  seek(t: number) {
    this.time = Math.min(this.duration, Math.max(0, t))
    for (const name of [...this.fired]) {
      if (this.time < (name === 'creak' ? 3 : name === 'birds' ? 18.2 : 24.4)) this.fired.delete(name)
    }
    this.applyTime(this.time)
    this.renderFrame()
  }

  skipToEnd() {
    this.time = 26.5
    this.frozen = false
    this.applyTime(this.time)
  }

  getTime(): number {
    return this.time
  }

  getDuration(): number {
    return this.duration
  }

  dispose() {
    this.stop()
    window.removeEventListener('pointermove', this.onPointerMove)
    document.removeEventListener('visibilitychange', this.onVisibility)
    this.ro.disconnect()
    this.stage.dispose()
    this.post.dispose()
    this.envRT.dispose()
    this.renderer.dispose()
    const el = this.renderer.domElement
    if (el.parentElement) el.parentElement.removeChild(el)
  }
}
