// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * LitBench 桌面客户端。
 *
 * 两种运行形态：
 *  - 个人版（打包了 Resources/backend）：启动内嵌 Python 服务端（uvicorn + SQLite），
 *    同时拉起后台 worker（消费上传/重解析 job），再把 /api 反代到它；首次打开无需任何
 *    服务端配置。worker 异常退出会写日志并自动重启。
 *  - 远程壳（没有 Resources/backend）：启动本地静态服务器，把 /api 反代到用户填的
 *    LitBench 服务端地址（适合团队部署）。
 *
 * 两种形态都不携带文献库、API Key、测试日志或偏好；数据只落在 Electron userData
 * （个人版的 SQLite 在 userData/data/）或远端服务端。
 * 为什么不用 file:// 直接加载：前端用相对 /api 调用；本地静态服务器 + 同源代理
 * 能保持前端代码零改动，也避免 Electron 的 file:// CORS 限制。
 */
const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require('electron')
const { spawn, spawnSync } = require('child_process')
const fs = require('fs')
const http = require('http')
const https = require('https')
const net = require('net')
const path = require('path')
const { URL } = require('url')

const DEFAULT_SERVER_URL = 'http://127.0.0.1:8000'
const RENDERER_DIR = path.join(__dirname, 'renderer')
const SETUP_HTML = path.join(__dirname, 'setup.html')
const SETUP_PRELOAD = path.join(__dirname, 'setup-preload.js')
const RESOURCES_DIR = process.resourcesPath || ''
const BACKEND_ROOT = path.join(RESOURCES_DIR, 'backend')
const EMBEDDED =
  fs.existsSync(path.join(BACKEND_ROOT, 'python', 'bin', 'python3')) ||
  fs.existsSync(path.join(BACKEND_ROOT, 'python', 'python.exe'))

let mainWindow = null
let setupWindow = null
let staticServer = null
let localBase = null
let config = null
let backendProcess = null
let backendLog = null
let workerProcess = null
let workerLog = null
let workerRestartTimer = null
let backendAlive = false
let quitting = false

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.wasm': 'application/wasm',
  '.map': 'application/json; charset=utf-8',
  '.pdf': 'application/pdf',
  '.txt': 'text/plain; charset=utf-8'
}

function configPath() {
  return path.join(app.getPath('userData'), 'litbench-client.json')
}

function loadConfig() {
  try {
    const raw = JSON.parse(fs.readFileSync(configPath(), 'utf8'))
    const url = normalizeServerUrl(raw.serverUrl || DEFAULT_SERVER_URL)
    return { serverUrl: url }
  } catch {
    return { serverUrl: DEFAULT_SERVER_URL }
  }
}

function saveConfig(next) {
  config = { serverUrl: normalizeServerUrl(next.serverUrl) }
  const file = configPath()
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, JSON.stringify(config, null, 2), { encoding: 'utf8', mode: 0o600 })
  try {
    fs.chmodSync(file, 0o600)
  } catch {
    // Windows 上 chmod 语义有限；macOS 上会生效
  }
}

function normalizeServerUrl(raw) {
  const value = String(raw || '').trim().replace(/\/+$/, '')
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    throw new Error('服务端地址必须是 http:// 或 https:// 开头的完整 URL')
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('服务端地址只支持 http:// 或 https://')
  }
  return parsed.toString().replace(/\/+$/, '')
}

function proxyApi(req, res) {
  let target
  try {
    // 支持服务端挂在反代子路径下（如 https://host/litbench）：把 /api/... 接到该前缀后。
    target = new URL(req.url.replace(/^\/+/, ''), `${config.serverUrl}/`)
    if (!['http:', 'https:'].includes(target.protocol)) {
      throw new Error('unsupported protocol')
    }
  } catch (err) {
    res.writeHead(400, { 'content-type': 'application/json; charset=utf-8' })
    res.end(JSON.stringify({ detail: `服务端地址无效：${err.message}` }))
    return
  }
  const client = target.protocol === 'https:' ? https : http
  const headers = { ...req.headers, host: target.host }
  const upstream = client.request(
    target,
    { method: req.method, headers },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers)
      upstreamRes.pipe(res)
    }
  )
  upstream.setTimeout(120000, () => {
    upstream.destroy(new Error('服务端响应超时（120s）'))
  })
  upstream.on('error', (err) => {
    if (!res.headersSent) {
      res.writeHead(502, { 'content-type': 'application/json; charset=utf-8' })
    }
    res.end(
      JSON.stringify({
        detail: `无法连接 LitBench 服务端（${config.serverUrl}）：${err.message}`
      })
    )
  })
  req.pipe(upstream)
}

function serveStatic(req, res) {
  let pathname = '/'
  try {
    pathname = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname)
  } catch {
    // 保底：非法 URL 当根路径
  }
  const candidate = path.normalize(path.join(RENDERER_DIR, pathname))
  if (!candidate.startsWith(RENDERER_DIR + path.sep) && candidate !== RENDERER_DIR) {
    res.writeHead(403)
    res.end('forbidden')
    return
  }
  let file = candidate
  if (!fs.existsSync(file) || !fs.statSync(file).isFile()) {
    // SPA 兜底：未知路径回 index.html（前端没有服务端路由，但避免 404 白屏）
    file = path.join(RENDERER_DIR, 'index.html')
  }
  if (!fs.existsSync(file)) {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' })
    res.end('LitBench 客户端资源缺失：renderer/index.html')
    return
  }
  const ext = path.extname(file).toLowerCase()
  const headers = {
    'content-type': MIME[ext] || 'application/octet-stream',
    'cache-control': ext === '.html' ? 'no-cache' : 'public, max-age=3600'
  }
  res.writeHead(200, headers)
  fs.createReadStream(file).pipe(res)
}

function startStaticServer() {
  return new Promise((resolve, reject) => {
    staticServer = http.createServer((req, res) => {
      const url = req.url || '/'
      if (url.startsWith('/api/') || url === '/api') {
        proxyApi(req, res)
        return
      }
      serveStatic(req, res)
    })
    staticServer.on('error', reject)
    staticServer.listen(0, '127.0.0.1', () => {
      const port = staticServer.address().port
      localBase = `http://127.0.0.1:${port}/`
      resolve(localBase)
    })
  })
}

function createSetupWindow() {
  if (setupWindow && !setupWindow.isDestroyed()) {
    setupWindow.focus()
    return setupWindow
  }
  setupWindow = new BrowserWindow({
    width: 620,
    height: 460,
    resizable: false,
    minimizable: false,
    maximizable: false,
    title: '连接 LitBench 服务端',
    show: false,
    webPreferences: {
      preload: SETUP_PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })
  setupWindow.setMenuBarVisibility(false)
  setupWindow.loadFile(SETUP_HTML, { query: { url: config.serverUrl } })
  setupWindow.once('ready-to-show', () => setupWindow.show())
  setupWindow.on('closed', () => {
    setupWindow = null
  })
  return setupWindow
}

function createMainWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show()
    mainWindow.focus()
    return mainWindow
  }
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 1024,
    minHeight: 700,
    title: 'LitBench',
    backgroundColor: '#f5f0e6',
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })
  mainWindow.once('ready-to-show', () => mainWindow.show())
  mainWindow.loadURL(localBase)
  mainWindow.on('closed', () => {
    mainWindow = null
  })
  // 外链交给系统浏览器；应用内不新开 Electron 窗口
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url)
    return { action: 'deny' }
  })
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(localBase)) {
      event.preventDefault()
      if (/^https?:/i.test(url)) shell.openExternal(url)
    }
  })
  return mainWindow
}

function buildMenu() {
  if (EMBEDDED) {
    const dataDir = path.join(app.getPath('userData'), 'data')
    const template = [
      {
        label: 'LitBench',
        submenu: [
          {
            label: '打开数据目录',
            click: () => shell.openPath(dataDir)
          },
          {
            label: '重新加载界面',
            accelerator: 'CmdOrCtrl+R',
            click: () => mainWindow && mainWindow.reload()
          },
          { type: 'separator' },
          { role: 'quit', label: '退出 LitBench' }
        ]
      },
      {
        label: '编辑',
        submenu: [
          { role: 'undo', label: '撤销' },
          { role: 'redo', label: '重做' },
          { type: 'separator' },
          { role: 'cut', label: '剪切' },
          { role: 'copy', label: '复制' },
          { role: 'paste', label: '粘贴' },
          { role: 'selectAll', label: '全选' }
        ]
      },
      {
        label: '视图',
        submenu: [
          { role: 'reload', label: '刷新' },
          { role: 'toggleDevTools', label: '开发者工具' },
          { type: 'separator' },
          { role: 'togglefullscreen', label: '全屏' }
        ]
      }
    ]
    Menu.setApplicationMenu(Menu.buildFromTemplate(template))
    return
  }
  const template = [
    {
      label: 'LitBench',
      submenu: [
        {
          label: '设置服务端地址…',
          accelerator: 'CmdOrCtrl+,',
          click: () => createSetupWindow()
        },
        { type: 'separator' },
        {
          label: '重新加载界面',
          accelerator: 'CmdOrCtrl+R',
          click: () => mainWindow && mainWindow.reload()
        },
        { type: 'separator' },
        { role: 'quit', label: '退出 LitBench' }
      ]
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' }
      ]
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '刷新' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' }
      ]
    }
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

function openMain() {
  if (localBase) createMainWindow()
}

// ---------------------------------------------------------------------------
// 内嵌服务端（个人版）：随 App 一起打包 Python 运行时 + 后端 + pandoc/typst
// ---------------------------------------------------------------------------
function findFreePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer()
    probe.unref()
    probe.on('error', reject)
    probe.listen(0, '127.0.0.1', () => {
      const port = probe.address().port
      probe.close(() => resolve(port))
    })
  })
}

function backendPaths() {
  const isWindows = process.platform === 'win32'
  return {
    python: isWindows
      ? path.join(BACKEND_ROOT, 'python', 'python.exe')
      : path.join(BACKEND_ROOT, 'python', 'bin', 'python3'),
    bin: path.join(BACKEND_ROOT, 'bin'),
    data: path.join(app.getPath('userData'), 'data'),
    log: path.join(app.getPath('userData'), 'logs', 'backend.log'),
    workerLog: path.join(app.getPath('userData'), 'logs', 'worker.log')
  }
}

function waitForBackend(port, timeoutMs = 90000) {
  const url = `http://127.0.0.1:${port}/api/ready`
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const tick = () => {
      http
        .get(url, (res) => {
          res.resume()
          if (res.statusCode === 200) resolve()
          else retry()
        })
        .on('error', retry)
    }
    const retry = () => {
      if (Date.now() > deadline) {
        reject(new Error(`本地服务端启动超时（${timeoutMs / 1000}s）`))
        return
      }
      setTimeout(tick, 400)
    }
    tick()
  })
}

function buildBackendEnv(paths) {
  const dbPath = path.join(paths.data, 'litbench.db')
  return {
    ...process.env,
    DATABASE_URL: `sqlite+aiosqlite:///${dbPath.replace(/\\/g, '/')}`,
    STORAGE_DIR: paths.data,
    AUTH_ENABLED: '0',
    PARSER_BACKEND: 'pdfium',
    ESCALATE_BACKEND: '',
    OCR_BACKEND: '',
    OCR_CROSS_ENABLED: '0',
    LITE_OCR_ENABLED: '1',
    OCR_CROSS_VALIDATE: '1',
    FILTER_QR_FIGURES: '1',
    RENDER_FIGURE_IMAGES: '1',
    CSL_ENABLED: '1',
    DOCX_EXPORT_ENABLED: '1',
    PDF_EXPORT_ENABLED: '1',
    PANDOC_PATH: path.join(
      paths.bin,
      process.platform === 'win32' ? 'pandoc.exe' : 'pandoc'
    ),
    TYPST_PATH: path.join(
      paths.bin,
      process.platform === 'win32' ? 'typst.exe' : 'typst'
    ),
    PDF_MAIN_FONT: process.platform === 'win32' ? 'SimSun' : 'Songti SC',
    PYTHONPATH: BACKEND_ROOT,
    PYTHONDONTWRITEBYTECODE: '1',
    PYTHONUNBUFFERED: '1',
    PATH: [paths.bin, path.dirname(paths.python), process.env.PATH || ''].join(
      path.delimiter
    )
  }
}

async function startEmbeddedBackend() {
  const paths = backendPaths()
  fs.mkdirSync(paths.data, { recursive: true })
  fs.mkdirSync(path.dirname(paths.log), { recursive: true })
  const dbPath = path.join(paths.data, 'litbench.db')
  const umaskSafe = () => {
    try {
      fs.chmodSync(dbPath, 0o600)
    } catch {
      // 首次创建前文件还不存在
    }
  }
  const env = buildBackendEnv(paths)

  // 1) 建表（全新 SQLite；不写示例数据）
  const boot = spawnSync(paths.python, [path.join(BACKEND_ROOT, 'bootstrap.py')], {
    cwd: BACKEND_ROOT,
    env,
    encoding: 'utf8',
    timeout: 120000
  })
  if (boot.status !== 0) {
    throw new Error(
      `本地数据库初始化失败：${boot.stderr || boot.stdout || `exit ${boot.status}`}`
    )
  }
  umaskSafe()

  // 2) 启动 uvicorn
  const port = await findFreePort()
  backendLog = fs.createWriteStream(paths.log, { flags: 'a' })
  backendProcess = spawn(
    paths.python,
    [
      '-m',
      'uvicorn',
      'litbench.main:app',
      '--host',
      '127.0.0.1',
      '--port',
      String(port),
      '--log-level',
      'warning'
    ],
    { cwd: BACKEND_ROOT, env }
  )
  backendProcess.stdout.pipe(backendLog)
  backendProcess.stderr.pipe(backendLog)
  backendProcess.on('exit', (code, signal) => {
    backendAlive = false
    if (quitting) return
    stopEmbeddedWorker()
    const detail = `本地服务端退出（code=${code}, signal=${signal || '-'}）`
    dialog.showErrorBox('LitBench 本地服务端已停止', `${detail}\n\n日志：${paths.log}`)
  })
  await waitForBackend(port)
  backendAlive = true
  // API 起来之后再起 worker：worker 消费上传/重解析 job，没有它任务会一直 queued
  startEmbeddedWorker(env, paths)
  config = { serverUrl: `http://127.0.0.1:${port}` }
}

function startEmbeddedWorker(env, paths) {
  if (workerProcess && !workerProcess.killed) return
  if (quitting) return
  workerLog = fs.createWriteStream(paths.workerLog, { flags: 'a' })
  workerProcess = spawn(
    paths.python,
    ['-m', 'litbench.workers.run', '--poll', '1'],
    { cwd: BACKEND_ROOT, env }
  )
  workerProcess.stdout.pipe(workerLog)
  workerProcess.stderr.pipe(workerLog)
  workerProcess.on('error', (err) => {
    try {
      fs.appendFileSync(
        paths.workerLog,
        `\n[litbench] worker 启动失败：${err.message}\n`
      )
    } catch {
      // ignore
    }
  })
  workerProcess.on('exit', (code, signal) => {
    workerProcess = null
    if (workerLog) {
      try {
        workerLog.end()
      } catch {
        // ignore
      }
      workerLog = null
    }
    if (quitting) return
    if (!backendAlive) return
    try {
      fs.appendFileSync(
        paths.workerLog,
        `\n[litbench] worker 退出（code=${code}, signal=${signal || '-'}），5 秒后重启\n`
      )
    } catch {
      // 日志写不进去也不能阻止重启
    }
    workerRestartTimer = setTimeout(() => {
      workerRestartTimer = null
      startEmbeddedWorker(env, paths)
    }, 5000)
  })
}

function stopEmbeddedWorker() {
  if (workerRestartTimer) {
    clearTimeout(workerRestartTimer)
    workerRestartTimer = null
  }
  if (workerProcess && !workerProcess.killed) {
    try {
      workerProcess.kill('SIGTERM')
    } catch {
      // ignore
    }
  }
  workerProcess = null
  if (workerLog) {
    try {
      workerLog.end()
    } catch {
      // ignore
    }
  }
  workerLog = null
}

function stopEmbeddedBackend() {
  stopEmbeddedWorker()
  if (backendProcess && !backendProcess.killed) {
    try {
      backendProcess.kill('SIGTERM')
    } catch {
      // ignore
    }
  }
  backendProcess = null
  if (backendLog) {
    try {
      backendLog.end()
    } catch {
      // ignore
    }
  }
  backendLog = null
}

app.whenReady().then(async () => {
  if (EMBEDDED) {
    try {
      await startEmbeddedBackend()
      await startStaticServer()
      buildMenu()
      openMain()
    } catch (err) {
      dialog.showErrorBox(
        'LitBench 启动失败',
        `${err && err.message ? err.message : String(err)}\n\n日志：${backendPaths().log}`
      )
      app.quit()
    }
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) openMain()
    })
    return
  }

  config = loadConfig()
  const hadConfig = fs.existsSync(configPath())
  if (!hadConfig) saveConfig(config)
  await startStaticServer()
  buildMenu()
  if (hadConfig) {
    openMain()
  } else {
    createSetupWindow()
  }
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      if (localBase) openMain()
      else createSetupWindow()
    }
  })
})

ipcMain.handle('litbench:get-server-url', () => config.serverUrl)

ipcMain.handle('litbench:save-server-url', (_event, rawUrl) => {
  let url
  try {
    url = normalizeServerUrl(rawUrl)
  } catch (err) {
    return { ok: false, error: err.message }
  }
  saveConfig({ serverUrl: url })
  if (setupWindow && !setupWindow.isDestroyed()) setupWindow.close()
  openMain()
  return { ok: true }
})

ipcMain.handle('litbench:cancel-setup', () => {
  if (setupWindow && !setupWindow.isDestroyed()) setupWindow.close()
  if (mainWindow) mainWindow.show()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  quitting = true
  stopEmbeddedBackend()
  if (staticServer) {
    try {
      staticServer.close()
    } catch {
      // ignore
    }
  }
})
