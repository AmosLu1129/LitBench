// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 后处理链（M6b）：Render → 光轴 → 景深 → Bloom → 胶片（暗角/颗粒/色差）→ 输出。
 * 用 EffectComposer + HalfFloat 目标，配合 ACES 色调映射。
 */
import * as THREE from 'three'
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js'
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'
import { BokehPass } from 'three/examples/jsm/postprocessing/BokehPass.js'
import { ShaderPass } from 'three/examples/jsm/postprocessing/ShaderPass.js'
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js'

/** 屏幕空间光轴：从太阳在屏幕上的位置向外做亮度加权径向采样 */
const GodRaysShader = {
  uniforms: {
    tDiffuse: { value: null as THREE.Texture | null },
    uSun: { value: new THREE.Vector2(0.7, 0.55) },
    uIntensity: { value: 0 },
    uColor: { value: new THREE.Color(0xffcf94) },
  },
  vertexShader: /* glsl */ `
    varying vec2 vUv;
    void main() {
      vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
  `,
  fragmentShader: /* glsl */ `
    uniform sampler2D tDiffuse;
    uniform vec2 uSun;
    uniform float uIntensity;
    uniform vec3 uColor;
    varying vec2 vUv;
    #define SAMPLES 44
    void main() {
      vec3 base = texture2D(tDiffuse, vUv).rgb;
      vec2 delta = (vUv - uSun) * (0.72 / float(SAMPLES));
      vec2 uv = vUv;
      vec3 acc = vec3(0.0);
      float w = 1.0;
      for (int i = 0; i < SAMPLES; i++) {
        uv -= delta;
        vec3 s = texture2D(tDiffuse, clamp(uv, 0.0, 1.0)).rgb;
        float lum = dot(s, vec3(0.299, 0.587, 0.114));
        float m = smoothstep(1.0, 1.9, lum);
        acc += s * m * w;
        w *= 0.965;
      }
      acc /= float(SAMPLES);
      gl_FragColor = vec4(base + acc * uColor * uIntensity, 1.0);
    }
  `,
}

/** 胶片收尾：暗角 + 颗粒 + 极轻色差 */
const FilmShader = {
  uniforms: {
    tDiffuse: { value: null as THREE.Texture | null },
    uTime: { value: 0 },
    uVignette: { value: 0.72 },
    uGrain: { value: 0.018 },
    uAberration: { value: 0.9 },
  },
  vertexShader: /* glsl */ `
    varying vec2 vUv;
    void main() {
      vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
  `,
  fragmentShader: /* glsl */ `
    uniform sampler2D tDiffuse;
    uniform float uTime;
    uniform float uVignette;
    uniform float uGrain;
    uniform float uAberration;
    varying vec2 vUv;
    float hash(vec2 p) {
      p = fract(p * vec2(443.897, 441.423));
      p += dot(p, p.yx + 19.19);
      return fract((p.x + p.y) * p.x);
    }
    void main() {
      vec2 c = vUv - 0.5;
      float r = length(c);
      vec2 off = c * r * r * 0.0055 * uAberration;
      vec3 col;
      col.r = texture2D(tDiffuse, vUv + off).r;
      col.g = texture2D(tDiffuse, vUv).g;
      col.b = texture2D(tDiffuse, vUv - off).b;
      float vig = smoothstep(0.92, 0.28, r * 1.18);
      col *= mix(1.0, vig, uVignette * 0.62);
      float g = hash(vUv * vec2(1920.0, 1080.0) + uTime * 61.7) - 0.5;
      col += g * uGrain * (1.0 - r * 0.7);
      gl_FragColor = vec4(col, 1.0);
    }
  `,
}

export interface Post {
  composer: EffectComposer
  bloom: UnrealBloomPass
  bokeh: BokehPass
  godrays: ShaderPass
  film: ShaderPass
  setSize: (w: number, h: number) => void
  update: (opts: { time: number; sun: THREE.Vector3; sunIntensity: number; focus: number; aperture: number }) => void
  dispose: () => void
}

export function createPost(
  renderer: THREE.WebGLRenderer,
  scene: THREE.Scene,
  camera: THREE.PerspectiveCamera,
): Post {
  const size = renderer.getSize(new THREE.Vector2())
  const dpr = renderer.getPixelRatio()
  const target = new THREE.WebGLRenderTarget(Math.max(1, size.x * dpr), Math.max(1, size.y * dpr), {
    type: THREE.HalfFloatType,
    samples: 4,
  })
  const composer = new EffectComposer(renderer, target)
  composer.setSize(size.x, size.y)

  const renderPass = new RenderPass(scene, camera)

  const godrays = new ShaderPass(GodRaysShader)
  godrays.uniforms.tDiffuse.value = null

  const bokeh = new BokehPass(scene, camera, {
    focus: 2.0,
    aperture: 0.0012,
    maxblur: 0.0045,
  })
  // BokehPass 内部会重新渲染一遍深度，放在光轴之后、Bloom 之前
  const bokehUniforms = (bokeh as unknown as { uniforms: Record<string, THREE.IUniform> }).uniforms

  const bloom = new UnrealBloomPass(new THREE.Vector2(size.x, size.y), 0.25, 0.6, 1.1)

  const film = new ShaderPass(FilmShader)
  const output = new OutputPass()

  composer.addPass(renderPass)
  composer.addPass(godrays)
  composer.addPass(bokeh)
  composer.addPass(bloom)
  composer.addPass(film)
  composer.addPass(output)

  const tmp = new THREE.Vector3()

  return {
    composer,
    bloom,
    bokeh,
    godrays,
    film,
    setSize(w: number, h: number) {
      composer.setSize(w, h)
      bloom.setSize(w, h)
    },
    update({ time, sun, sunIntensity, focus, aperture }) {
      tmp.copy(sun).project(camera)
      godrays.uniforms.uSun.value.set(tmp.x * 0.5 + 0.5, tmp.y * 0.5 + 0.5)
      godrays.uniforms.uIntensity.value = sunIntensity
      if (bokehUniforms.focus) bokehUniforms.focus.value = focus
      if (bokehUniforms.aperture) bokehUniforms.aperture.value = aperture
      film.uniforms.uTime.value = time
    },
    dispose() {
      composer.dispose()
      target.dispose()
    },
  }
}
