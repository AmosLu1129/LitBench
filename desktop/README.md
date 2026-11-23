# LitBench 桌面客户端

这里提供两种桌面形态：

1. **个人版（内置服务端，推荐）**：Electron 壳 + 内嵌 Python 运行时 + 后端 + pandoc/typst。
   打开 App 会自动在 `127.0.0.1` 启动本地服务端、建全新 SQLite 库，并**同时拉起后台
   worker**（解析上传/重解析任务）；worker 异常退出会写 `logs/worker.log` 并自动重启。
2. **远程壳（thin client）**：只带前端，连接你填写的 LitBench 服务端；适合团队部署。

两种都只打包客户端代码；构建产物不包含 `.data/`、测试文献库、模型 API Key、使用日志或偏好。
个人版首次启动会在 `~/Library/Application Support/<AppName>/data/` 新建空库，之后的数据归用户自己。

## 个人版：macOS arm64 dmg

需要预先准备（任选 macOS 或 Linux/WSL 执行打包）：

- Electron `darwin-arm64` zip（如 `electron-v33.0.0-darwin-arm64.zip`）
- CPython standalone `aarch64-apple-darwin` tar.gz（如
  `cpython-3.12.16+...-aarch64-apple-darwin-install_only.tar.gz`）
- 为 macOS arm64 下载的 Python wheels（`pip download --platform macosx_13_0_arm64 ...`）
- pandoc `arm64-macOS.zip`、typst `aarch64-apple-darwin.tar.xz`

```bash
# 1) 前端构建
cd web && npm ci && npm run build && cd ..

# 2) 打包（在 macOS 或 Linux/WSL 上）
./desktop/build-macos-embedded.sh \
  --electron-zip /path/electron-v33.0.0-darwin-arm64.zip \
  --python-tar /path/cpython-3.12.16+...-aarch64-apple-darwin-install_only.tar.gz \
  --wheels-dir /path/macos-arm64-wheels \
  --pandoc-zip /path/pandoc-3.14-arm64-macOS.zip \
  --typst-tar /path/typst-0.15.1-aarch64-apple-darwin.tar.xz \
  --out dist/LitBench-0.1.0-arm64.dmg
```

macOS 上用系统 `hdiutil` 产标准 dmg；Linux/WSL 上用 `xorrisofs` + `libdmg-hfsplus` 的
`dmg` 交叉打包（产物未签名）。

个人版内置的是 **pdfium 轻量解析档**：opencv/Pillow 已带，二维码过滤与缩略图可用；
**不含 Docling / MinerU**。Windows 包额外带 **RapidOCR + ONNX Runtime**（扫描页 OCR）；
macOS 包当前仍是 pdfium-only（要 OCR 需后续加 wheel 重打）。

## 个人版：Windows x64

在 Windows 本机执行（Electron / Python 运行时 / wheels 都是 win32-x64）：

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build-windows-x64.ps1 `
  -ElectronZip .data\tmp\win-assets\electron-win.zip `
  -PythonTar .data\tmp\win-assets\python-win.tar.gz `
  -WheelsDir .data\tmp\win-wheels `
  -PandocZip .data\tmp\win-assets\pandoc-win.zip `
  -TypstZip .data\tmp\win-assets\typst-win.zip `
  -NsisZip .data\tmp\win-assets\nsis-3.11.zip `
  -OutDir dist
```

产出：
- `LitBench-0.1.0-x64-portable.zip`：解压后直接运行 `LitBench.exe`；
- `LitBench-0.1.0-x64-setup.exe`：NSIS 安装器（安装到 `%LOCALAPPDATA%\LitBench`，不需要管理员权限）。

安装器会写入 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench`
（DisplayName / DisplayVersion / InstallLocation / DisplayIcon / UninstallString /
QuietUninstallString / EstimatedSize / NoModify / NoRepair / InstallDate），
因此会出现在「设置 → 应用」/ 控制面板「卸载程序」里，可在那里一键卸载；
卸载会删除安装目录、快捷方式和注册表项，但**保留用户文献库** `%APPDATA%\LitBench`。

Windows 版内置的后端与 mac 版相同（python.exe + 后端 + pandoc/typst），另含
RapidOCR + ONNX Runtime；数据在 `%APPDATA%\<AppName>\data\`（首次为空），日志在
`...\logs\backend.log` 与 `...\logs\worker.log`。

> 安装器在非交互环境里可能无法自动静默安装（当前构建机没有交互桌面）；
> 压缩包是可直接验证的便携形态。

## 远程壳（thin client）

```bash
./desktop/build-macos.sh \
  --electron-zip /path/electron-v33.0.0-darwin-arm64.zip \
  --out dist/LitBench-0.1.0-arm64.dmg
```

启动后填服务端地址；`/api` 由本地静态服务器反向代理到该地址。

## 未签名说明

两种 dmg 都未做 Apple Developer 签名/公证。首次打开可能被 Gatekeeper 拦截：

```bash
xattr -dr com.apple.quarantine /Applications/LitBench.app
open /Applications/LitBench.app
```

或右键 App →「打开」→ 确认。正式分发需要 Developer ID 证书 + `codesign` + `notarytool`。

## MCP 包（单独分发）

MCP 是 MIT 薄层，不随桌面客户端安装，单独产出：

- `dist/litbench_mcp-0.1.0-py3-none-any.whl`：标准 Python wheel；
- `dist/LitBench-MCP-0.1.0.zip`：wheel + Windows 安装说明 + 示例 MCP 配置 + MIT LICENSE。

安装与接入见 `mcp/INSTALL-WINDOWS.md`；它通过 HTTP 连 LitBench API（默认
`http://127.0.0.1:8000`），不依赖 Electron 客户端。

## 客户端保存了什么

- 个人版：SQLite、PDF/图片等都在 `~/Library/Application Support/<AppName>/data/`，首次为空；
  后端/worker 日志在 `.../logs/backend.log`、`.../logs/worker.log`。不随安装包携带任何测试数据。
- 远程壳：只保存服务端 URL（`.../litbench-client.json`，0600），不保存登录凭据。
