// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * M6a 环境音：全部用 WebAudio 现场合成，不依赖音频文件。
 * 浏览器策略：没有用户手势之前 AudioContext 无法出声，所以必须先 enable()。
 */
export type SoundName = 'creak' | 'birds' | 'page'

export class WelcomeAudio {
  private ctx: AudioContext | null = null
  private master: GainNode | null = null
  private noise: AudioBuffer | null = null
  private muted: boolean

  constructor(muted: boolean) {
    this.muted = muted
  }

  get running(): boolean {
    return this.ctx?.state === 'running'
  }

  /** 必须在用户手势里调用一次 */
  async enable(): Promise<boolean> {
    if (!this.ctx) {
      const Ctor =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
      if (!Ctor) return false
      this.ctx = new Ctor()
      this.master = this.ctx.createGain()
      this.master.gain.value = this.muted ? 0 : 0.85
      this.master.connect(this.ctx.destination)
      this.noise = this.buildNoise(this.ctx)
    }
    if (this.ctx.state === 'suspended') await this.ctx.resume()
    return this.ctx.state === 'running'
  }

  setMuted(muted: boolean) {
    this.muted = muted
    if (this.master && this.ctx) {
      this.master.gain.setTargetAtTime(muted ? 0 : 0.85, this.ctx.currentTime, 0.08)
    }
  }

  play(name: SoundName) {
    if (!this.ctx || !this.master || !this.noise || this.muted) return
    if (this.ctx.state !== 'running') return
    if (name === 'creak') this.creak()
    else if (name === 'birds') this.birds()
    else this.page()
  }

  private buildNoise(ctx: AudioContext): AudioBuffer {
    const len = Math.floor(ctx.sampleRate * 2)
    const buf = ctx.createBuffer(1, len, ctx.sampleRate)
    const data = buf.getChannelData(0)
    let s = 12345
    for (let i = 0; i < len; i++) {
      s = (s * 1103515245 + 12345) & 0x7fffffff
      data[i] = (s / 0x3fffffff - 1) * 0.9
    }
    return buf
  }

  private source(): AudioBufferSourceNode | null {
    if (!this.ctx || !this.noise) return null
    const src = this.ctx.createBufferSource()
    src.buffer = this.noise
    src.loop = true
    return src
  }

  /** 老木轴的干涩摩擦声：带通噪声 + 慢速扫频 + 颗粒感幅度 */
  private creak() {
    const ctx = this.ctx
    if (!ctx) return
    const now = ctx.currentTime
    const src = this.source()
    if (!src) return
    const bp = ctx.createBiquadFilter()
    bp.type = 'bandpass'
    bp.Q.value = 7
    bp.frequency.setValueAtTime(190, now)
    bp.frequency.linearRampToValueAtTime(95, now + 1.7)
    const hp = ctx.createBiquadFilter()
    hp.type = 'highpass'
    hp.frequency.value = 60
    const g = ctx.createGain()
    g.gain.setValueAtTime(0.0001, now)
    g.gain.exponentialRampToValueAtTime(0.09, now + 0.22)
    g.gain.exponentialRampToValueAtTime(0.045, now + 1.1)
    g.gain.exponentialRampToValueAtTime(0.0001, now + 1.75)
    src.connect(bp).connect(hp).connect(g).connect(this.master!)
    src.start(now)
    src.stop(now + 1.8)

    // 轻微低频"木头张力"
    const osc = ctx.createOscillator()
    osc.type = 'triangle'
    osc.frequency.setValueAtTime(58, now)
    osc.frequency.linearRampToValueAtTime(41, now + 1.6)
    const og = ctx.createGain()
    og.gain.setValueAtTime(0.0001, now)
    og.gain.exponentialRampToValueAtTime(0.035, now + 0.3)
    og.gain.exponentialRampToValueAtTime(0.0001, now + 1.7)
    osc.connect(og).connect(this.master!)
    osc.start(now)
    osc.stop(now + 1.8)
  }

  /** 鸟鸣：三声短促扫频 */
  private birds() {
    const ctx = this.ctx
    if (!ctx) return
    for (let i = 0; i < 3; i++) {
      const t0 = ctx.currentTime + i * 0.16 + Math.random() * 0.04
      const osc = ctx.createOscillator()
      osc.type = 'sine'
      const f0 = 2600 + Math.random() * 900
      osc.frequency.setValueAtTime(f0, t0)
      osc.frequency.exponentialRampToValueAtTime(f0 * 1.35, t0 + 0.05)
      osc.frequency.exponentialRampToValueAtTime(f0 * 0.9, t0 + 0.11)
      const g = ctx.createGain()
      g.gain.setValueAtTime(0.0001, t0)
      g.gain.exponentialRampToValueAtTime(0.045, t0 + 0.02)
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.13)
      osc.connect(g).connect(this.master!)
      osc.start(t0)
      osc.stop(t0 + 0.15)
    }
  }

  /** 翻页：短促高频噪声 */
  private page() {
    const ctx = this.ctx
    if (!ctx) return
    const now = ctx.currentTime
    const src = this.source()
    if (!src) return
    const hp = ctx.createBiquadFilter()
    hp.type = 'highpass'
    hp.frequency.setValueAtTime(1400, now)
    hp.frequency.linearRampToValueAtTime(2600, now + 0.4)
    const g = ctx.createGain()
    g.gain.setValueAtTime(0.0001, now)
    g.gain.exponentialRampToValueAtTime(0.055, now + 0.05)
    g.gain.exponentialRampToValueAtTime(0.0001, now + 0.45)
    src.connect(hp).connect(g).connect(this.master!)
    src.start(now)
    src.stop(now + 0.5)
  }
}
