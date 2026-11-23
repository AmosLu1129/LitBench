// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * M6b 舞台：真三维的窗外（山脊 / 树 / 桃枝 / 鸟）、会跳的火苗与青烟、
 * 金属与环境反射、纸面的微小起伏与更细的冰裂纹窗棂。
 */
import * as THREE from 'three'
import {
  makeBlossomTexture,
  makeBumpTexture,
  makeContactShadowTexture,
  makeDustTexture,
  makeEnvEquirect,
  makeGlowTexture,
  makeMistTexture,
  makePaperTexture,
  makePetalTexture,
  makePlasterTexture,
  makeScrollTexture,
  makeWoodTexture,
} from './textures'

export interface Sash {
  pivot: THREE.Group
  dir: 1 | -1
}

export interface Bird {
  root: THREE.Group
  wings: THREE.Mesh[]
  phase: number
  speed: number
  y: number
  z: number
}

export interface Stage {
  root: THREE.Group
  sashes: Sash[]
  sun: THREE.DirectionalLight
  coolFill: THREE.DirectionalLight
  lamp: THREE.PointLight
  flameMats: THREE.ShaderMaterial[]
  smokeMat: THREE.ShaderMaterial
  dust: THREE.Points
  dustMat: THREE.PointsMaterial
  mistMats: THREE.MeshBasicMaterial[]
  wallGlowMat: THREE.MeshBasicMaterial
  /** 太阳在世界里的位置（给屏幕空间光轴做锚点） */
  sunPos: THREE.Vector3
  /** 天空散射着色器（随黎明整体提亮） */
  skyMat: THREE.ShaderMaterial
  birds: Bird[]
  petals: THREE.InstancedMesh
  petalSeeds: Float32Array
  scrollCenter: THREE.Vector3
  envTexture: THREE.Texture
  /** 0 = 黎明前（窗外全暗）, 1 = 天光大亮 */
  setDaylight: (k: number) => void
  /** 火苗/青烟/鸟/花瓣的逐帧动画 */
  setTime: (t: number) => void
  dispose: () => void
}

function pick(seed: number): () => number {  let s = seed >>> 0
  return () => {
    s = (s + 0x6d2b79f5) >>> 0
    let t = Math.imul(s ^ (s >>> 15), 1 | s)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function smoothstep(edge0: number, edge1: number, x: number): number {
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)))
  return t * t * (3 - 2 * t)
}

interface Bar {
  x: number
  y: number
  len: number
  angle: number
}

/** 冰裂纹：抖动网格连线，越靠边越规整 */
function crackBars(w: number, h: number, cols: number, rows: number, seed: number): Bar[] {
  const r = pick(seed)
  const pts: { x: number; y: number }[][] = []
  const cw = w / (cols - 1)
  const ch = h / (rows - 1)
  for (let j = 0; j < rows; j++) {
    const row: { x: number; y: number }[] = []
    for (let i = 0; i < cols; i++) {
      row.push({ x: -w / 2 + i * cw + (r() - 0.5) * cw * 0.34, y: -h / 2 + j * ch + (r() - 0.5) * ch * 0.34 })
    }
    pts.push(row)
  }
  const bars: Bar[] = []
  const add = (a: { x: number; y: number }, b: { x: number; y: number }) => {
    const dx = b.x - a.x
    const dy = b.y - a.y
    const len = Math.hypot(dx, dy)
    if (len < 0.02) return
    bars.push({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, len, angle: Math.atan2(dy, dx) })
  }
  for (let j = 0; j < rows; j++) {
    for (let i = 0; i < cols; i++) {
      const p = pts[j][i]
      if (i + 1 < cols && r() > 0.1) add(p, pts[j][i + 1])
      if (j + 1 < rows && r() > 0.12) add(p, pts[j + 1][i])
      if (i + 1 < cols && j + 1 < rows && r() > 0.86) add(p, pts[j + 1][i + 1])
    }
  }
  return bars
}

function crackMesh(bars: Bar[], barW: number, barD: number, mat: THREE.Material): THREE.InstancedMesh {
  const geo = new THREE.BoxGeometry(1, 1, 1)
  const mesh = new THREE.InstancedMesh(geo, mat, bars.length)
  const m = new THREE.Matrix4()
  const q = new THREE.Quaternion()
  const axis = new THREE.Vector3(0, 0, 1)
  const pos = new THREE.Vector3()
  const scl = new THREE.Vector3()
  bars.forEach((b, i) => {
    q.setFromAxisAngle(axis, b.angle)
    pos.set(b.x, b.y, 0)
    scl.set(b.len, barW, barD)
    m.compose(pos, q, scl)
    mesh.setMatrixAt(i, m)
  })
  mesh.instanceMatrix.needsUpdate = true
  mesh.castShadow = true
  mesh.receiveShadow = true
  return mesh
}

function box(w: number, h: number, d: number, mat: THREE.Material, x: number, y: number, z: number): THREE.Mesh {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat)
  m.position.set(x, y, z)
  m.castShadow = true
  m.receiveShadow = true
  return m
}

/** 圆角矩形截面：给砚台、镇尺做倒角 */
function roundedRectShape(w: number, h: number, r: number): THREE.Shape {
  const s = new THREE.Shape()
  s.moveTo(-w / 2 + r, -h / 2)
  s.lineTo(w / 2 - r, -h / 2)
  s.quadraticCurveTo(w / 2, -h / 2, w / 2, -h / 2 + r)
  s.lineTo(w / 2, h / 2 - r)
  s.quadraticCurveTo(w / 2, h / 2, w / 2 - r, h / 2)
  s.lineTo(-w / 2 + r, h / 2)
  s.quadraticCurveTo(-w / 2, h / 2, -w / 2, h / 2 - r)
  s.lineTo(-w / 2, -h / 2 + r)
  s.quadraticCurveTo(-w / 2, -h / 2, -w / 2 + r, -h / 2)
  return s
}

/** 一条有起伏、渐细的枝（用三段管近似） */
function branchTube(points: THREE.Vector3[], radius: number, mat: THREE.Material): THREE.Mesh[] {
  const curve = new THREE.CatmullRomCurve3(points)
  const segs: THREE.Mesh[] = []
  for (let i = 0; i < 3; i++) {
    const a = i / 3
    const b = (i + 1) / 3
    const sub = new THREE.CatmullRomCurve3([
      curve.getPoint(a),
      curve.getPoint(a + (b - a) * 0.5),
      curve.getPoint(b),
    ])
    const r = radius * (1 - a * 0.62)
    const geo = new THREE.TubeGeometry(sub, 12, r, 6, false)
    const mesh = new THREE.Mesh(geo, mat)
    mesh.castShadow = true
    segs.push(mesh)
  }
  return segs
}

/** 值噪声 + fbm：给地形做起伏 */
function hash2(x: number, y: number): number {
  const s = Math.sin(x * 127.1 + y * 311.7) * 43758.5453
  return s - Math.floor(s)
}

function vnoise(x: number, y: number): number {
  const xi = Math.floor(x)
  const yi = Math.floor(y)
  const xf = x - xi
  const yf = y - yi
  const u = xf * xf * (3 - 2 * xf)
  const v = yf * yf * (3 - 2 * yf)
  const a = hash2(xi, yi)
  const b = hash2(xi + 1, yi)
  const c = hash2(xi, yi + 1)
  const d = hash2(xi + 1, yi + 1)
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v
}

function fbm(x: number, y: number, oct = 4): number {
  let v = 0
  let amp = 0.5
  let f = 1
  for (let i = 0; i < oct; i++) {
    v += vnoise(x * f, y * f) * amp
    f *= 2.03
    amp *= 0.5
  }
  return v
}

/** 窗外地形高度：近处是谷底，远处起伏成山 */
function terrainHeight(x: number, z: number): number {
  const d = -z
  const valley = 0.02 + 0.18 * (fbm(x * 0.05, z * 0.05, 2) - 0.5)
  const near = smoothstep(6, 28, d)
  const hills = (fbm(x * 0.03 + 11, z * 0.03 - 7, 4) - 0.34) * 11.0
  const far = smoothstep(22, 70, d)
  const range = (fbm(x * 0.012 + 3, z * 0.012 + 5, 3) - 0.44) * 26.0
  return valley + near * hills + far * range
}

/** 天空球：散射渐变 + 太阳盘 + 地平暖带 */
const SKY_VERT = /* glsl */ `
  varying vec3 vDir;
  void main() {
    vDir = position;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`

const SKY_FRAG = /* glsl */ `
  uniform vec3 uSunDir;
  uniform vec3 uZenith;
  uniform vec3 uHorizon;
  uniform float uDaylight;
  varying vec3 vDir;

  void main() {
    vec3 dir = normalize(vDir);
    float up = clamp(dir.y, 0.0, 1.0);
    vec3 sky = mix(uHorizon, uZenith, pow(up, 0.55));

    float sd = max(dot(dir, uSunDir), 0.0);
    float glow = pow(sd, 18.0) * 0.28 + pow(sd, 120.0) * 0.85;
    float disc = smoothstep(0.9997, 0.99992, sd);
    vec3 sunCol = vec3(1.0, 0.84, 0.6);

    float horizon = pow(max(1.0 - up, 0.0), 5.0);
    vec3 col = sky * (0.25 + 0.75 * uDaylight);
    col += sunCol * glow * uDaylight * 1.1;
    col += sunCol * disc * 3.2 * uDaylight;
    col += vec3(0.95, 0.6, 0.34) * horizon * pow(sd, 1.4) * 0.32 * uDaylight;
    gl_FragColor = vec4(col, 1.0);
  }
`

/** 远处的小树剪影（真几何，给窗外纵深） */
function makeTree(x: number, y: number, z: number, s: number, mat: THREE.Material): THREE.Group {
  const g = new THREE.Group()
  const trunk = new THREE.Mesh(new THREE.CylinderGeometry(0.008 * s, 0.014 * s, 0.34 * s, 6), mat)
  trunk.position.y = 0.17 * s
  g.add(trunk)
  const crownParts: [number, number, number][] = [
    [0, 0.42, 0.62],
    [0.1, 0.34, 0.5],
    [-0.09, 0.32, 0.46],
  ]
  for (const [dx, dy, sc] of crownParts) {
    const cone = new THREE.Mesh(new THREE.ConeGeometry(0.16 * s * sc, 0.4 * s * sc, 7), mat)
    cone.position.set(dx * s, dy * s, 0)
    g.add(cone)
  }
  g.position.set(x, y, z)
  return g
}

export function buildStage(): Stage {
  const root = new THREE.Group()
  const textures: THREE.Texture[] = []
  const track = <T extends THREE.Texture>(t: T): T => {
    textures.push(t)
    return t
  }

  const woodTex = track(makeWoodTexture())
  const paperTex = track(makePaperTexture())
  const scrollTex = track(makeScrollTexture())
  const dustTex = track(makeDustTexture())
  const glowTex = track(makeGlowTexture())
  const mistTex = track(makeMistTexture())
  const plasterTex = track(makePlasterTexture())
  const paperBump = track(makeBumpTexture('paper', 5))
  const woodBump = track(makeBumpTexture('wood', 11))
  const plasterBump = track(makeBumpTexture('plaster', 17))
  const envTexture = track(makeEnvEquirect())
  const blossomTex = track(makeBlossomTexture())
  const petalTex = track(makePetalTexture())
  const shadowTex = track(makeContactShadowTexture())

  const M = {
    wood: new THREE.MeshStandardMaterial({
      map: woodTex,
      bumpMap: woodBump,
      bumpScale: 0.012,
      color: 0x8a7058,
      roughness: 0.58,
      metalness: 0.04,
      envMapIntensity: 0.5,
    }),
    darkWood: new THREE.MeshStandardMaterial({
      color: 0x2e2118,
      bumpMap: woodBump,
      bumpScale: 0.02,
      roughness: 0.66,
      metalness: 0.02,
    }),
    wall: new THREE.MeshStandardMaterial({
      map: plasterTex,
      bumpMap: plasterBump,
      bumpScale: 0.008,
      color: 0xc8b49a,
      roughness: 0.97,
    }),
    paper: new THREE.MeshStandardMaterial({
      map: paperTex,
      bumpMap: paperBump,
      bumpScale: 0.006,
      color: 0xd8cbaf,
      roughness: 0.94,
    }),
    scroll: new THREE.MeshStandardMaterial({
      map: scrollTex,
      bumpMap: paperBump,
      bumpScale: 0.007,
      color: 0xc9bba2,
      roughness: 0.95,
      metalness: 0,
      envMapIntensity: 0.2,
    }),
    copper: new THREE.MeshStandardMaterial({ color: 0xb07a45, roughness: 0.3, metalness: 0.92, envMapIntensity: 1.1 }),
    brass: new THREE.MeshStandardMaterial({ color: 0x6b5528, roughness: 0.5, metalness: 0.9, envMapIntensity: 0.3 }),
    porcelain: new THREE.MeshPhysicalMaterial({
      color: 0x8fa39a,
      roughness: 0.22,
      metalness: 0,
      clearcoat: 1,
      clearcoatRoughness: 0.12,
      envMapIntensity: 1.0,
    }),
    stone: new THREE.MeshStandardMaterial({
      color: 0x2b2823,
      bumpMap: plasterBump,
      bumpScale: 0.005,
      roughness: 0.58,
      metalness: 0.05,
    }),
    lacquer: new THREE.MeshPhysicalMaterial({
      color: 0x6e2318,
      roughness: 0.26,
      metalness: 0,
      clearcoat: 0.9,
      clearcoatRoughness: 0.2,
      envMapIntensity: 0.8,
    }),
    ink: new THREE.MeshStandardMaterial({ color: 0x0e0c0b, roughness: 0.08, metalness: 0.2, envMapIntensity: 1.2 }),
    shadow: new THREE.MeshBasicMaterial({ map: shadowTex, transparent: true, depthWrite: false }),
    wallGlow: new THREE.MeshBasicMaterial({
      map: track(makeGlowTexture()),
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      opacity: 0,
      fog: false,
      color: 0xffc98a,
    }),
    mist: new THREE.MeshBasicMaterial({ map: mistTex, transparent: true, opacity: 0, depthWrite: false, fog: false }),
    blossom: new THREE.MeshStandardMaterial({
      map: blossomTex,
      color: 0x9a655c,
      emissive: 0xd08a7e,
      emissiveIntensity: 0.55,
      transparent: true,
      alphaTest: 0.28,
      depthWrite: true,
      side: THREE.DoubleSide,
      roughness: 1,
      metalness: 0,
      fog: false,
    }),
    petal: new THREE.MeshBasicMaterial({
      map: petalTex,
      transparent: true,
      alphaTest: 0.15,
      depthWrite: false,
      side: THREE.DoubleSide,
      fog: false,
    }),
  }

  // ---------- 桌 ----------
  const deskGeo = new THREE.BoxGeometry(1.5, 0.06, 0.8)
  const desk = new THREE.Mesh(deskGeo, M.wood)
  desk.position.set(0, -0.03, 0.03)
  desk.castShadow = true
  desk.receiveShadow = true
  root.add(desk)

  // 桌沿倒角（一条更细的木边，避免"一块砖"）
  const edge = new THREE.Mesh(new THREE.BoxGeometry(1.51, 0.008, 0.81), M.darkWood)
  edge.position.set(0, -0.004, 0.03)
  root.add(edge)

  // 接触阴影：让每件东西真的"坐"在桌面上
  const contact = (w: number, h: number, x: number, z: number, opacity: number) => {
    const mat = M.shadow.clone()
    mat.opacity = opacity
    const p = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mat)
    p.rotation.x = -Math.PI / 2
    p.position.set(x, 0.0035, z)
    root.add(p)
    return p
  }
  contact(0.84, 0.34, 0.02, -0.12, 0.55)
  contact(0.17, 0.17, -0.52, -0.04, 0.5)
  contact(0.09, 0.09, -0.2, 0.24, 0.4)
  contact(0.21, 0.16, 0.62, -0.18, 0.45)
  contact(0.16, 0.12, -0.31, 0.1, 0.32)

  // ---------- 墙与窗 ----------
  root.add(box(2.4, 3.2, 0.12, M.wall, -2.0, 1.6, -1.28))
  root.add(box(2.4, 3.2, 0.12, M.wall, 2.0, 1.6, -1.28))
  root.add(box(1.6, 0.55, 0.12, M.wall, 0, 0.275, -1.28))
  root.add(box(1.6, 1.65, 0.12, M.wall, 0, 2.375, -1.28))

  root.add(box(0.05, 1.0, 0.16, M.darkWood, -0.775, 1.05, -1.28))
  root.add(box(0.05, 1.0, 0.16, M.darkWood, 0.775, 1.05, -1.28))
  root.add(box(1.6, 0.05, 0.16, M.darkWood, 0, 1.525, -1.28))
  root.add(box(1.72, 0.06, 0.3, M.darkWood, 0, 0.525, -1.22))

  // 窗台薄尘
  const sillDust = new THREE.Mesh(new THREE.PlaneGeometry(1.6, 0.22), M.mist)
  sillDust.rotation.x = -Math.PI / 2
  sillDust.position.set(0, 0.557, -1.22)
  root.add(sillDust)

  const transom = new THREE.Group()
  const tw = 1.5
  const th = 0.44
  transom.add(box(tw, 0.04, 0.05, M.darkWood, 0, th / 2 - 0.02, 0))
  transom.add(box(tw, 0.04, 0.05, M.darkWood, 0, -th / 2 + 0.02, 0))
  transom.add(box(0.04, th, 0.05, M.darkWood, -tw / 2 + 0.02, 0, 0))
  transom.add(box(0.04, th, 0.05, M.darkWood, tw / 2 - 0.02, 0, 0))
  transom.add(crackMesh(crackBars(tw - 0.08, th - 0.08, 26, 9, 5), 0.0052, 0.016, M.darkWood))
  transom.position.set(0, 1.275, -1.28)
  root.add(transom)

  const sashes: Sash[] = []
  const sh = 0.46
  const sw = 0.75
  const makeSash = (dir: 1 | -1): Sash => {
    const pivot = new THREE.Group()
    pivot.position.set(dir * 0.75, 0.8, -1.28)
    const panel = new THREE.Group()
    panel.position.set(-dir * (sw / 2), 0, 0)
    panel.add(box(sw, 0.045, 0.04, M.darkWood, 0, sh / 2 - 0.0225, 0))
    panel.add(box(sw, 0.045, 0.04, M.darkWood, 0, -sh / 2 + 0.0225, 0))
    panel.add(box(0.045, sh, 0.04, M.darkWood, -sw / 2 + 0.0225, 0, 0))
    panel.add(box(0.045, sh, 0.04, M.darkWood, sw / 2 - 0.0225, 0, 0))
    panel.add(crackMesh(crackBars(sw - 0.09, sh - 0.09, 10, 6, dir > 0 ? 11 : 17), 0.005, 0.014, M.darkWood))
    pivot.add(panel)
    root.add(pivot)
    return { pivot, dir }
  }
  sashes.push(makeSash(1), makeSash(-1))

  // ---------- 天空球（散射） ----------
  const sunPos = new THREE.Vector3(-1.9, 2.6, -13)
  const sunDir = sunPos.clone().normalize()
  const skyMat = new THREE.ShaderMaterial({
    uniforms: {
      uSunDir: { value: sunDir },
      uZenith: { value: new THREE.Color(0x101620) },
      uHorizon: { value: new THREE.Color(0x3c3730) },
      uDaylight: { value: 0 },
    },
    vertexShader: SKY_VERT,
    fragmentShader: SKY_FRAG,
    side: THREE.BackSide,
    depthWrite: false,
    fog: false,
  })
  const skyDome = new THREE.Mesh(new THREE.SphereGeometry(160, 48, 24), skyMat)
  root.add(skyDome)

  // ---------- 地形（真高度场） ----------
  const terrainGeo = new THREE.PlaneGeometry(180, 130, 220, 150)
  terrainGeo.rotateX(-Math.PI / 2)
  {
    const pos = terrainGeo.attributes.position
    for (let i = 0; i < pos.count; i++) {
      const x = pos.getX(i)
      const z = pos.getZ(i) - 66
      pos.setZ(i, z)
      pos.setY(i, terrainHeight(x, z))
    }
    terrainGeo.computeVertexNormals()

    const colors = new Float32Array(pos.count * 3)
    const nrm = terrainGeo.attributes.normal
    const cNear = new THREE.Color(0x0d1512)
    const cMid = new THREE.Color(0x1e2b28)
    const cFar = new THREE.Color(0x50625f)
    const cWarm = new THREE.Color(0x7a5a3e)
    const toSun = new THREE.Vector3(-1.9, 2.6, -13).normalize()
    const tmp = new THREE.Color()
    const n = new THREE.Vector3()
    for (let i = 0; i < pos.count; i++) {
      const x = pos.getX(i)
      const dist = -pos.getZ(i)
      const h = pos.getY(i)
      const haze = smoothstep(14, 130, dist)
      n.set(nrm.getX(i), nrm.getY(i), nrm.getZ(i)).normalize()
      const light = Math.max(0, n.dot(toSun))
      tmp.copy(cNear).lerp(cMid, smoothstep(6, 50, dist)).lerp(cFar, haze * 0.9)
      tmp.multiplyScalar(0.42 + light * 0.85)
      tmp.lerp(cWarm, haze * Math.max(0, -x / 70) * 0.35)
      tmp.multiplyScalar(0.9 + Math.min(1, h / 8) * 0.2)
      colors[i * 3] = tmp.r
      colors[i * 3 + 1] = tmp.g
      colors[i * 3 + 2] = tmp.b
    }
    terrainGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3))
  }
  const terrainMat = new THREE.MeshBasicMaterial({ vertexColors: true, fog: false })
  root.add(new THREE.Mesh(terrainGeo, terrainMat))

  const treeMat = new THREE.MeshBasicMaterial({ color: 0x1a2528, fog: false })
  const treeSpots: [number, number, number][] = [
    [-2.6, -5.4, 1.5],
    [1.9, -4.4, 1.1],
    [-4.2, -8.2, 1.9],
    [3.6, -9.0, 2.1],
    [-8.5, -13.5, 2.6],
    [7.8, -15.0, 2.8],
  ]
  for (const [tx, tz, ts] of treeSpots) {
    root.add(makeTree(tx, terrainHeight(tx, tz) + 0.02, tz, ts, treeMat))
  }

  // 雾带（窗外不同深度各一条，做出分层空气感）
  const mistMats: THREE.MeshBasicMaterial[] = []
  const mistDefs: [number, number, number, number, number][] = [
    [8, 1.1, 0, 0.85, -9.5],
    [12, 1.5, 0, 1.0, -14],
    [18, 2.0, 0, 1.3, -20],
  ]
  for (const [w, h, x, y, z] of mistDefs) {
    const mat = M.mist.clone()
    mat.opacity = 0
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mat)
    m.position.set(x, y, z)
    root.add(m)
    mistMats.push(mat)
  }

  // ---------- 桃枝（真三维：枝 + 花簇 + 花瓣），斜穿窗景 ----------
  const branchMat = new THREE.MeshStandardMaterial({ color: 0x33251c, roughness: 0.88 })
  const branchPts = [
    new THREE.Vector3(1.05, 0.3, -1.42),
    new THREE.Vector3(0.62, 0.62, -1.44),
    new THREE.Vector3(0.26, 0.9, -1.46),
    new THREE.Vector3(-0.14, 1.12, -1.48),
    new THREE.Vector3(-0.5, 1.3, -1.5),
  ]
  for (const seg of branchTube(branchPts, 0.026, branchMat)) root.add(seg)
  const twigA = [branchPts[1], new THREE.Vector3(0.78, 0.94, -1.46), new THREE.Vector3(0.86, 1.18, -1.48)]
  for (const seg of branchTube(twigA, 0.012, branchMat)) root.add(seg)
  const twigB = [branchPts[2], new THREE.Vector3(0.46, 1.16, -1.48), new THREE.Vector3(0.5, 1.38, -1.5)]
  for (const seg of branchTube(twigB, 0.01, branchMat)) root.add(seg)
  const twigC = [branchPts[3], new THREE.Vector3(0.04, 1.34, -1.5), new THREE.Vector3(0.08, 1.56, -1.52)]
  for (const seg of branchTube(twigC, 0.009, branchMat)) root.add(seg)
  const twigD = [branchPts[4], new THREE.Vector3(-0.68, 1.5, -1.52), new THREE.Vector3(-0.76, 1.7, -1.54)]
  for (const seg of branchTube(twigD, 0.008, branchMat)) root.add(seg)
  // 低处一根横枝，让花落在窗洞里
  const branchPts2 = [
    new THREE.Vector3(0.98, 0.56, -1.4),
    new THREE.Vector3(0.56, 0.66, -1.42),
    new THREE.Vector3(0.18, 0.74, -1.44),
    new THREE.Vector3(-0.06, 0.8, -1.46),
  ]
  for (const seg of branchTube(branchPts2, 0.015, branchMat)) root.add(seg)
  const twigF = [branchPts2[1], new THREE.Vector3(0.66, 0.88, -1.42), new THREE.Vector3(0.74, 1.02, -1.44)]
  for (const seg of branchTube(twigF, 0.008, branchMat)) root.add(seg)

  // 花簇：沿枝撒点
  const blossomGeo = new THREE.PlaneGeometry(0.06, 0.06)
  const blossomCount = 300
  const blossoms = new THREE.InstancedMesh(blossomGeo, M.blossom, blossomCount)
  {
    const r = pick(41)
    const m = new THREE.Matrix4()
    const q = new THREE.Quaternion()
    const e = new THREE.Euler()
    const p = new THREE.Vector3()
    const s = new THREE.Vector3()
    const paths = [branchPts, twigA, twigB, twigC, twigD, branchPts2, twigF]
    for (let i = 0; i < blossomCount; i++) {
      const path = paths[Math.floor(r() * paths.length)]
      const t = r()
      const idx = Math.min(path.length - 1, Math.floor(t * (path.length - 1)))
      const base = path[idx]
      p.set(base.x + (r() - 0.5) * 0.2, base.y + (r() - 0.5) * 0.2, base.z + (r() - 0.5) * 0.08)
      e.set(r() * Math.PI, r() * Math.PI, r() * Math.PI)
      q.setFromEuler(e)
      const sc = 0.75 + r() * 0.9
      s.set(sc, sc, sc)
      m.compose(p, q, s)
      blossoms.setMatrixAt(i, m)
    }
    blossoms.instanceMatrix.needsUpdate = true
  }
  root.add(blossoms)

  // 飘落花瓣
  const petalGeo = new THREE.PlaneGeometry(0.026, 0.018)
  const petalCount = 34
  const petals = new THREE.InstancedMesh(petalGeo, M.petal, petalCount)
  petals.instanceMatrix.setUsage(THREE.DynamicDrawUsage)
  root.add(petals)
  const petalSeeds = new Float32Array(petalCount * 5)
  {
    const r = pick(59)
    for (let i = 0; i < petalCount; i++) {
      petalSeeds[i * 5] = 0.1 + r() * 1.5
      petalSeeds[i * 5 + 1] = 0.55 + r() * 1.1
      petalSeeds[i * 5 + 2] = -1.3 - r() * 0.8
      petalSeeds[i * 5 + 3] = r() * Math.PI * 2
      petalSeeds[i * 5 + 4] = 0.15 + r() * 0.35
    }
  }

  // ---------- 鸟（真三维：身体 + 两片翅） ----------
  const birds: Bird[] = []
  const birdMat = new THREE.MeshBasicMaterial({ color: 0x14100d, fog: false, side: THREE.DoubleSide })
  const birdDefs: [number, number, number, number, number][] = [
    [0.2, 1.32, -5.2, 0.9, 0.0],
    [-0.5, 1.45, -7.4, 1.25, 2.1],
    [0.75, 1.2, -3.6, 0.7, 4.0],
  ]
  for (const [x, y, z, speed, phase] of birdDefs) {
    const g = new THREE.Group()
    const body = new THREE.Mesh(new THREE.ConeGeometry(0.018, 0.11, 6), birdMat)
    body.rotation.z = Math.PI / 2
    g.add(body)
    const wings: THREE.Mesh[] = []
    for (const side of [-1, 1]) {
      const pivotWing = new THREE.Group()
      const wing = new THREE.Mesh(new THREE.PlaneGeometry(0.16, 0.055), birdMat)
      wing.position.x = side * 0.08
      pivotWing.add(wing)
      g.add(pivotWing)
      wings.push(wing)
    }
    g.position.set(x, y, z)
    g.rotation.y = Math.PI / 2
    g.scale.setScalar(0.55)
    root.add(g)
    birds.push({ root: g, wings, phase, speed, y, z })
  }

  // ---------- 书卷 ----------
  const scrollGeo = new THREE.PlaneGeometry(0.72, 0.26, 48, 16)
  {
    const pos = scrollGeo.attributes.position
    for (let i = 0; i < pos.count; i++) {
      const x = pos.getX(i)
      const y = pos.getY(i)
      const curl = Math.pow(Math.abs(x) / 0.36, 3.2) * 0.014
      const wave = Math.sin(x * 15) * 0.0016 + Math.sin(y * 11 + x * 4) * 0.0009
      pos.setZ(i, curl + wave)
    }
    scrollGeo.computeVertexNormals()
  }
  const scroll = new THREE.Mesh(scrollGeo, M.scroll)
  scroll.rotation.x = -Math.PI / 2
  scroll.position.set(0.02, 0.006, -0.12)
  scroll.castShadow = true
  scroll.receiveShadow = true
  root.add(scroll)

  const rollerGeo = new THREE.CylinderGeometry(0.015, 0.015, 0.3, 20)
  for (const sx of [-1, 1]) {
    const roller = new THREE.Mesh(rollerGeo, M.lacquer)
    roller.rotation.x = Math.PI / 2
    roller.position.set(0.02 + sx * 0.366, 0.018, -0.12)
    roller.castShadow = true
    roller.receiveShadow = true
    root.add(roller)
    const knob = new THREE.Mesh(new THREE.CylinderGeometry(0.019, 0.019, 0.022, 16), M.brass)
    knob.rotation.x = Math.PI / 2
    knob.position.set(0.02 + sx * 0.366, 0.018, -0.12 + 0.15 * (sx > 0 ? 1 : -1))
    root.add(knob)
  }

  // ---------- 油灯 ----------
  const lampGroup = new THREE.Group()
  lampGroup.position.set(-0.52, 0, -0.04)
  const profile: [number, number][] = [
    [0.0, 0.0],
    [0.058, 0.0],
    [0.062, 0.014],
    [0.054, 0.034],
    [0.046, 0.058],
    [0.034, 0.074],
    [0.022, 0.082],
    [0.02, 0.096],
    [0.024, 0.104],
  ]
  const latheGeo = new THREE.LatheGeometry(
    profile.map(([r, y]) => new THREE.Vector2(r, y)),
    28,
  )
  const lampBody = new THREE.Mesh(latheGeo, M.copper)
  lampBody.castShadow = true
  lampBody.receiveShadow = true
  lampGroup.add(lampBody)

  const wick = new THREE.Mesh(new THREE.CylinderGeometry(0.0035, 0.0035, 0.022, 8), M.ink)
  wick.position.y = 0.112
  lampGroup.add(wick)
  root.add(lampGroup)

  // 火苗：两层带噪声的 billboard
  type FlameUniforms = { uTime: { value: number }; uSeed: { value: number }; uHeight: { value: number } }
  const flameUniforms: FlameUniforms[] = []
  const flameMats: THREE.ShaderMaterial[] = []
  const flameShader = (seed: number, height: number): THREE.ShaderMaterial => {
    const u: FlameUniforms = {
      uTime: { value: 0 },
      uSeed: { value: seed },
      uHeight: { value: height },
    }
    flameUniforms.push(u)
    const mat = new THREE.ShaderMaterial({
      uniforms: u,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      side: THREE.DoubleSide,
      vertexShader: /* glsl */ `
        varying vec2 vUv;
        void main() {
          vUv = uv;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: /* glsl */ `
        uniform float uTime;
        uniform float uSeed;
        uniform float uHeight;
        varying vec2 vUv;
        float hash(vec2 p) {
          p = fract(p * vec2(123.34, 345.45));
          p += dot(p, p + 34.345);
          return fract(p.x * p.y);
        }
        float noise(vec2 p) {
          vec2 i = floor(p);
          vec2 f = fract(p);
          vec2 u = f * f * (3.0 - 2.0 * f);
          return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
                     mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
        }
        void main() {
          vec2 uv = vUv;
          float y = uv.y;
          // 泪滴外形
          float width = 0.30 * pow(sin(clamp(y, 0.0, 1.0) * 3.14159), 0.7);
          float wob = (noise(vec2(uv.x * 5.0 + uSeed, y * 4.0 - uTime * 2.6)) - 0.5) * 0.09;
          float dx = abs(uv.x - 0.5 + wob);
          float body = smoothstep(width, width * 0.25, dx);
          float top = smoothstep(1.0, 0.55, y);
          float bottom = smoothstep(0.0, 0.12, y);
          float a = body * top * bottom;
          a *= 0.75 + 0.25 * sin(uTime * 9.0 + y * 8.0);
          vec3 core = vec3(1.0, 0.86, 0.55);
          vec3 edge = vec3(1.0, 0.42, 0.12);
          vec3 col = mix(edge, core, smoothstep(0.0, 0.75, a));
          gl_FragColor = vec4(col, a * 0.95);
          if (a < 0.01) discard;
        }
      `,
    })
    flameMats.push(mat)
    return mat
  }
  const flame1 = new THREE.Mesh(new THREE.PlaneGeometry(0.042, 0.075), flameShader(1.0, 0.075))
  flame1.position.set(-0.52, 0.152, -0.04)
  root.add(flame1)
  const flame2 = new THREE.Mesh(new THREE.PlaneGeometry(0.06, 0.098), flameShader(7.3, 0.098))
  flame2.position.set(-0.52, 0.163, -0.041)
  root.add(flame2)

  const flameGlow = new THREE.Sprite(
    new THREE.SpriteMaterial({
      map: glowTex,
      color: 0xffcf94,
      transparent: true,
      opacity: 0.85,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      fog: false,
    }),
  )
  flameGlow.position.set(-0.52, 0.158, -0.04)
  flameGlow.scale.set(0.34, 0.42, 1)
  root.add(flameGlow)

  // 青烟
  const smokeUniforms: { uTime: THREE.IUniform } = { uTime: { value: 0 } }
  const smokeMat = new THREE.ShaderMaterial({
    uniforms: smokeUniforms,
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    vertexShader: /* glsl */ `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      varying vec2 vUv;
      float hash(vec2 p) {
        p = fract(p * vec2(127.1, 311.7));
        p += dot(p, p + 19.19);
        return fract(p.x * p.y);
      }
      float noise(vec2 p) {
        vec2 i = floor(p);
        vec2 f = fract(p);
        vec2 u = f * f * (3.0 - 2.0 * f);
        return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
                   mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);
      }
      void main() {
        vec2 uv = vUv;
        float sway = (noise(vec2(uv.y * 2.5 - uTime * 0.32, uv.x * 3.0)) - 0.5) * 0.7;
        float d = abs(uv.x - 0.5 - sway * uv.y);
        float body = smoothstep(0.3 * (1.0 - uv.y * 0.5), 0.0, d);
        float vert = smoothstep(1.0, 0.15, uv.y) * smoothstep(0.0, 0.2, uv.y);
        float n = noise(vec2(uv.x * 6.0 + uTime * 0.2, uv.y * 5.0 - uTime * 0.5));
        float a = body * vert * smoothstep(0.15, 0.75, n) * 0.02;
        gl_FragColor = vec4(vec3(0.5, 0.46, 0.4), a);
      }
    `,
  })
  const smoke = new THREE.Mesh(new THREE.PlaneGeometry(0.1, 0.55), smokeMat)
  smoke.position.set(-0.52, 0.5, -0.04)
  root.add(smoke)

  // ---------- 茶盏 / 砚 / 笔 / 镇纸 / 书堆 ----------
  const cupProfile: THREE.Vector2[] = []
  for (let i = 0; i <= 10; i++) {
    const t = i / 10
    cupProfile.push(new THREE.Vector2(0.012 + Math.sin(t * Math.PI * 0.5) * 0.028, t * 0.036))
  }
  const cup = new THREE.Mesh(new THREE.LatheGeometry(cupProfile, 20), M.porcelain)
  cup.position.set(-0.2, 0.002, 0.24)
  cup.castShadow = true
  root.add(cup)
  const tea = new THREE.Mesh(new THREE.CircleGeometry(0.03, 20), M.ink)
  tea.rotation.x = -Math.PI / 2
  tea.position.set(-0.2, 0.03, 0.24)
  root.add(tea)

  // 砚台：圆角石砚（倒角挤出）+ 墨池 + 池沿 + 笔舔
  const inkstoneGeo = new THREE.ExtrudeGeometry(roundedRectShape(0.13, 0.09, 0.012), {
    depth: 0.013,
    bevelEnabled: true,
    bevelSegments: 3,
    bevelSize: 0.0025,
    bevelThickness: 0.0025,
    steps: 1,
  })
  inkstoneGeo.rotateX(-Math.PI / 2)
  inkstoneGeo.translate(0, 0.003, 0)
  const inkstone = new THREE.Mesh(inkstoneGeo, M.stone)
  inkstone.position.set(-0.31, 0, 0.1)
  inkstone.castShadow = true
  inkstone.receiveShadow = true
  root.add(inkstone)

  const inkPool = new THREE.Mesh(new THREE.CircleGeometry(0.03, 40), M.ink)
  inkPool.rotation.x = -Math.PI / 2
  inkPool.scale.set(1, 1.3, 1)
  inkPool.position.set(-0.31, 0.0187, 0.1)
  root.add(inkPool)
  const inkRim = new THREE.Mesh(new THREE.TorusGeometry(0.031, 0.0015, 8, 48), M.stone)
  inkRim.rotation.x = -Math.PI / 2
  inkRim.scale.set(1, 1.3, 1)
  inkRim.position.set(-0.31, 0.0193, 0.1)
  root.add(inkRim)
  root.add(box(0.055, 0.0035, 0.011, M.ink, -0.31, 0.0195, 0.143))

  const brushRest = box(0.1, 0.014, 0.024, M.darkWood, 0.38, 0.007, 0.06)
  root.add(brushRest)
  const brushHandle = new THREE.Mesh(new THREE.CylinderGeometry(0.0045, 0.0055, 0.2, 10), M.darkWood)
  brushHandle.rotation.z = Math.PI / 2
  brushHandle.rotation.y = 0.14
  brushHandle.position.set(0.38, 0.019, 0.06)
  brushHandle.castShadow = true
  root.add(brushHandle)
  const bristle = new THREE.Mesh(new THREE.ConeGeometry(0.009, 0.045, 10), M.ink)
  bristle.rotation.z = -Math.PI / 2
  bristle.position.set(0.49, 0.019, 0.072)
  root.add(bristle)

  const weightGeo = new THREE.ExtrudeGeometry(roundedRectShape(0.115, 0.03, 0.007), {
    depth: 0.015,
    bevelEnabled: true,
    bevelSegments: 3,
    bevelSize: 0.002,
    bevelThickness: 0.002,
    steps: 1,
  })
  weightGeo.rotateX(-Math.PI / 2)
  weightGeo.translate(0, 0.004, 0)
  const bronze = new THREE.MeshStandardMaterial({
    color: 0x4b3d2c,
    roughness: 0.36,
    metalness: 0.88,
    envMapIntensity: 0.6,
  })
  const weight = new THREE.Mesh(weightGeo, bronze)
  weight.position.set(-0.06, 0, 0.1)
  weight.castShadow = true
  weight.receiveShadow = true
  root.add(weight)
  root.add(box(0.088, 0.0012, 0.002, M.ink, -0.06, 0.0205, 0.089))
  root.add(box(0.088, 0.0012, 0.002, M.ink, -0.06, 0.0205, 0.111))
  for (const sx of [-1, 1]) {
    const cap = new THREE.Mesh(new THREE.CylinderGeometry(0.0055, 0.0065, 0.011, 16), bronze)
    cap.rotation.z = Math.PI / 2
    cap.position.set(-0.06 + sx * 0.056, 0.0175, 0.1)
    root.add(cap)
  }

  const bookMats = [
    new THREE.MeshStandardMaterial({ color: 0x3a2f26, roughness: 0.8 }),
    new THREE.MeshStandardMaterial({ color: 0x4a3a2c, roughness: 0.8 }),
    new THREE.MeshStandardMaterial({ color: 0x2c241d, roughness: 0.8 }),
  ]
  for (let i = 0; i < 3; i++) {
    const bk = box(0.16 - i * 0.012, 0.016, 0.11 - i * 0.008, bookMats[i], 0.62, 0.008 + i * 0.016, -0.18)
    bk.rotation.y = 0.15 - i * 0.08
    root.add(bk)
  }

  // ---------- 尘埃 ----------
  const dustCount = 900
  const dustPos = new Float32Array(dustCount * 3)
  const r = pick(97)
  for (let i = 0; i < dustCount; i++) {
    dustPos[i * 3] = (r() - 0.5) * 1.6
    dustPos[i * 3 + 1] = 0.08 + r() * 1.25
    dustPos[i * 3 + 2] = -1.1 + r() * 1.05
  }
  const dustGeo = new THREE.BufferGeometry()
  dustGeo.setAttribute('position', new THREE.BufferAttribute(dustPos, 3))
  const dustMat = new THREE.PointsMaterial({
    map: dustTex,
    size: 0.015,
    transparent: true,
    opacity: 0,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    color: 0xffd9a8,
    fog: false,
    sizeAttenuation: true,
  })
  const dust = new THREE.Points(dustGeo, dustMat)
  root.add(dust)

  // ---------- 墙上的回弹光 ----------
  const wallGlow = new THREE.Mesh(new THREE.PlaneGeometry(7.5, 6.2), M.wallGlow)
  wallGlow.position.set(0, 1.2, -1.19)
  root.add(wallGlow)

  // ---------- 灯 ----------
  root.add(new THREE.HemisphereLight(0x6d7a86, 0x1a1310, 0.42))
  root.add(new THREE.AmbientLight(0x3a2f28, 0.8))

  const lamp = new THREE.PointLight(0xff9a45, 2.2, 2.0, 2)
  lamp.position.set(-0.52, 0.17, -0.04)
  lamp.castShadow = true
  lamp.shadow.mapSize.set(1024, 1024)
  lamp.shadow.bias = -0.0012
  lamp.shadow.normalBias = 0.02
  root.add(lamp)

  const bounce = new THREE.PointLight(0xffb27a, 0.6, 2.6, 2)
  bounce.position.set(0.3, 0.42, 0.12)
  root.add(bounce)

  const coolFill = new THREE.DirectionalLight(0x8fa8cc, 0.22)
  coolFill.position.set(0.2, 1.3, -1.2)
  coolFill.target.position.set(0, 0.3, 0.2)
  root.add(coolFill, coolFill.target)

  const sun = new THREE.DirectionalLight(0xffd2a1, 0)
  sun.position.set(-1.4, 5.9, -6.2)
  sun.target.position.set(0, 0.5, -0.3)
  sun.castShadow = true
  sun.shadow.mapSize.set(2048, 2048)
  sun.shadow.camera.near = 1
  sun.shadow.camera.far = 18
  sun.shadow.camera.left = -1.8
  sun.shadow.camera.right = 1.8
  sun.shadow.camera.top = 1.8
  sun.shadow.camera.bottom = -1.8
  sun.shadow.bias = -0.0005
  sun.shadow.normalBias = 0.012
  root.add(sun, sun.target)

  const dispose = () => {
    root.traverse((obj) => {
      const mesh = obj as THREE.Mesh
      if (mesh.geometry) mesh.geometry.dispose()
      const mat = (mesh as unknown as { material?: THREE.Material | THREE.Material[] }).material
      if (Array.isArray(mat)) mat.forEach((m) => m.dispose())
      else if (mat) mat.dispose()
    })
    textures.forEach((t) => t.dispose())
  }

  // ---------- 窗外随黎明整体提亮 ----------
  const tint: { mat: THREE.MeshBasicMaterial | THREE.MeshStandardMaterial; base: THREE.Color }[] = [
    { mat: treeMat, base: treeMat.color.clone() },
    { mat: terrainMat, base: terrainMat.color.clone() },
    { mat: M.blossom, base: M.blossom.color.clone() },
    { mat: M.petal, base: M.petal.color.clone() },
  ]

  const setDaylight = (k: number) => {
    for (const t of tint) t.mat.color.copy(t.base).multiplyScalar(k)
    skyMat.uniforms.uDaylight.value = k
  }

  const setTime = (t: number) => {
    for (const u of flameUniforms) u.uTime.value = t
    smokeUniforms.uTime.value = t
    // 鸟：扇翅 + 缓慢横移
    for (const b of birds) {
      b.root.position.x = b.phase * 0.3 + Math.sin(t * 0.22 + b.phase) * 1.9 - 0.3
      b.root.position.y = b.y + Math.sin(t * 0.7 + b.phase) * 0.03
      const flap = Math.sin(t * 9.5 + b.phase) * 0.85
      b.wings[0].parent!.rotation.z = flap
      b.wings[1].parent!.rotation.z = -flap
    }
    // 花瓣：下落 + 翻转
    const m = new THREE.Matrix4()
    const q = new THREE.Quaternion()
    const e = new THREE.Euler()
    const p = new THREE.Vector3()
    const s = new THREE.Vector3(1, 1, 1)
    for (let i = 0; i < petalSeeds.length / 5; i++) {
      const bx = petalSeeds[i * 5]
      const by = petalSeeds[i * 5 + 1]
      const bz = petalSeeds[i * 5 + 2]
      const ph = petalSeeds[i * 5 + 3]
      const sp = petalSeeds[i * 5 + 4]
      const fall = (t * sp * 0.18 + ph) % 1.6
      const x = bx + Math.sin(t * 0.9 + ph) * 0.06
      const y = by + 0.5 - fall
      const z = bz + Math.cos(t * 0.6 + ph) * 0.03
      e.set(t * 1.7 + ph, t * 1.1 + ph * 2, Math.sin(t + ph) * 0.8)
      q.setFromEuler(e)
      p.set(x, y, z)
      s.setScalar(0.8 + Math.sin(ph) * 0.25)
      m.compose(p, q, s)
      petals.setMatrixAt(i, m)
    }
    petals.instanceMatrix.needsUpdate = true
  }

  return {
    root,
    sashes,
    sun,
    coolFill,
    lamp,
    flameMats,
    smokeMat,
    dust,
    dustMat,
    mistMats,
    wallGlowMat: M.wallGlow,
    sunPos,
    skyMat,
    birds,
    petals,
    petalSeeds,
    scrollCenter: new THREE.Vector3(0.02, 0.01, -0.12),
    envTexture,
    setDaylight,
    setTime,
    dispose,
  }
}
