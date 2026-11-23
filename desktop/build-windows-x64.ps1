# SPDX-License-Identifier: AGPL-3.0-or-later
# 构建 Windows x64 个人版：Electron 壳 + 内嵌 Python 服务端 + pandoc/typst。
# 只在 Windows 本机执行（Electron/py 运行时都是 win32-x64）。
#
# 用法（仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File desktop/build-windows-x64.ps1 `
#     -ElectronZip .data\tmp\win-assets\electron-win.zip `
#     -PythonTar .data\tmp\win-assets\python-win.tar.gz `
#     -WheelsDir .data\tmp\win-wheels `
#     -PandocZip .data\tmp\win-assets\pandoc-win.zip `
#     -TypstZip .data\tmp\win-assets\typst-win.zip `
#     -NsisZip .data\tmp\win-assets\nsis-3.10.zip `
#     -OutDir dist
param(
  [Parameter(Mandatory = $true)][string]$ElectronZip,
  [Parameter(Mandatory = $true)][string]$PythonTar,
  [Parameter(Mandatory = $true)][string]$WheelsDir,
  [Parameter(Mandatory = $true)][string]$PandocZip,
  [Parameter(Mandatory = $true)][string]$TypstZip,
  [string]$NsisZip = "",
  [string]$OutDir = "dist",
  [string]$Version = "0.1.0",
  [string]$Renderer = "web\dist"
)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutDir = [System.IO.Path]::GetFullPath((Join-Path $Repo $OutDir))
$BuildRoot = Join-Path $Repo ".data\tmp\win-build"
$Stage = Join-Path $BuildRoot "LitBench"
$App = Join-Path $Stage "resources\app"
$Backend = Join-Path $Stage "resources\backend"

function Reset-Dir([string]$Path) {
  if (Test-Path $Path) { Remove-Item -Recurse -Force $Path }
  New-Item -ItemType Directory -Force -Path $Path | Out-Null
}

Write-Host "== 清理/解压 Electron"
Reset-Dir $BuildRoot
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
$electronTmp = Join-Path $BuildRoot "electron-unzip"
New-Item -ItemType Directory -Force -Path $electronTmp | Out-Null
Expand-Archive -Path $ElectronZip -DestinationPath $electronTmp -Force
if (Test-Path (Join-Path $electronTmp "electron.exe")) {
  $electronDir = Get-Item $electronTmp
} else {
  $electronDir = Get-ChildItem $electronTmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName "electron.exe") } | Select-Object -First 1
}
if (-not $electronDir) { throw "Electron zip 里没有 electron.exe" }
Copy-Item -Path (Join-Path $electronDir.FullName "*") -Destination $Stage -Recurse -Force
if (Test-Path (Join-Path $Stage "resources\default_app.asar")) {
  Remove-Item -Force (Join-Path $Stage "resources\default_app.asar")
}
$exe = Join-Path $Stage "electron.exe"
if (Test-Path $exe) { Rename-Item -Path $exe -NewName "LitBench.exe" }

Write-Host "== 客户端壳 + 前端"
Reset-Dir $App
Copy-Item (Join-Path $Repo "desktop\package.json"), (Join-Path $Repo "desktop\main.js"), (Join-Path $Repo "desktop\setup.html"), (Join-Path $Repo "desktop\setup-preload.js") -Destination $App -Force
Copy-Item -Path (Join-Path $Repo $Renderer) -Destination (Join-Path $App "renderer") -Recurse -Force

Write-Host "== Python 运行时"
$pyTmp = Join-Path $BuildRoot "py"
New-Item -ItemType Directory -Force -Path $pyTmp | Out-Null
tar -xzf $PythonTar -C $pyTmp
$pyRoot = Get-ChildItem $pyTmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName "python.exe") } | Select-Object -First 1
if (-not $pyRoot) { throw "Python tar 结构不认识" }
New-Item -ItemType Directory -Force -Path (Join-Path $Backend "python") | Out-Null
Copy-Item -Path (Join-Path $pyRoot.FullName "*") -Destination (Join-Path $Backend "python") -Recurse -Force
# 运行时不需要：调试符号 / 头文件 / 静态库 / 构建脚本 / Tcl
Get-ChildItem -Path (Join-Path $Backend "python") -Recurse -Filter "*.pdb" | Remove-Item -Force
foreach ($unused in @("include", "libs", "Scripts", "tcl")) {
  $p = Join-Path $Backend "python\$unused"
  if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

Write-Host "== wheels → Lib\site-packages"
$site = Join-Path $Backend "python\Lib\site-packages"
New-Item -ItemType Directory -Force -Path $site | Out-Null
$wheels = Get-ChildItem $WheelsDir -Filter "*.whl"
$wheelTmp = Join-Path $BuildRoot "wheels-zip"
New-Item -ItemType Directory -Force -Path $wheelTmp | Out-Null
foreach ($wheel in $wheels) {
  # Expand-Archive 只认 .zip 扩展名；把 wheel 复制成 .zip 再解
  $zipCopy = Join-Path $wheelTmp ($wheel.BaseName + ".zip")
  Copy-Item $wheel.FullName $zipCopy -Force
  Expand-Archive -Path $zipCopy -DestinationPath $site -Force
}
Write-Host "   $($wheels.Count) wheels"
# 轻量 OCR：RapidOCR + ONNX Runtime 必须进包，否则扫描件只会停在 needs_ocr。
if (-not (Test-Path (Join-Path $site "rapidocr"))) { throw "wheels 缺少 rapidocr" }
if (-not (Test-Path (Join-Path $site "onnxruntime"))) { throw "wheels 缺少 onnxruntime" }
if (-not (Test-Path (Join-Path $site "cv2"))) { throw "wheels 缺少 opencv（rapidocr 依赖）" }
# 运行后端不需要 pip/setuptools/wheel，去掉减体积
foreach ($pattern in @("pip", "pip-*", "setuptools", "setuptools-*", "wheel", "wheel-*")) {
  Get-ChildItem -Path $site -Filter $pattern -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
}

Write-Host "== 后端源码"
$src = Join-Path $Repo "server\litbench"
$dst = Join-Path $Backend "litbench"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
Get-ChildItem -Path $dst -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Copy-Item (Join-Path $Repo "desktop\bootstrap.py") -Destination (Join-Path $Backend "bootstrap.py") -Force

Write-Host "== pandoc / typst"
$bin = Join-Path $Backend "bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$pandocTmp = Join-Path $BuildRoot "pandoc"
Expand-Archive -Path $PandocZip -DestinationPath $pandocTmp -Force
$pandoc = Get-ChildItem $pandocTmp -Recurse -Filter "pandoc.exe" | Select-Object -First 1
if (-not $pandoc) { throw "pandoc zip 里没有 pandoc.exe" }
Copy-Item $pandoc.FullName (Join-Path $bin "pandoc.exe") -Force
$typstTmp = Join-Path $BuildRoot "typst"
Expand-Archive -Path $TypstZip -DestinationPath $typstTmp -Force
$typst = Get-ChildItem $typstTmp -Recurse -Filter "typst.exe" | Select-Object -First 1
if (-not $typst) { throw "typst zip 里没有 typst.exe" }
Copy-Item $typst.FullName (Join-Path $bin "typst.exe") -Force

Write-Host "== 许可证"
Copy-Item (Join-Path $Repo "LICENSE") (Join-Path $Stage "resources\LICENSE") -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $Repo "LICENSING.md") (Join-Path $Stage "resources\LICENSING.md") -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $Repo "THIRD_PARTY.md") (Join-Path $Stage "resources\THIRD_PARTY.md") -Force -ErrorAction SilentlyContinue

Write-Host "== 应用图标"
$ico = Join-Path $Repo "desktop\icon.ico"
if (Test-Path $ico) {
  # Electron 的 exe 图标替换需要 rcedit；没有就保留默认图标，不影响功能。
  Write-Host "   icon.ico 存在（安装器会使用；应用 exe 默认图标暂不替换）"
}

Write-Host "== 便携目录检查"
if (-not (Test-Path (Join-Path $Stage "LitBench.exe"))) { throw "缺少 LitBench.exe" }
if (-not (Test-Path (Join-Path $Backend "python\python.exe"))) { throw "缺少内嵌 python.exe" }
if (-not (Test-Path (Join-Path $Backend "bin\pandoc.exe"))) { throw "缺少 pandoc.exe" }
if (-not (Test-Path (Join-Path $Backend "bin\typst.exe"))) { throw "缺少 typst.exe" }
if (-not (Test-Path (Join-Path $site "rapidocr"))) { throw "缺少 rapidocr（轻量 OCR）" }
if (-not (Test-Path (Join-Path $site "onnxruntime"))) { throw "缺少 onnxruntime（轻量 OCR）" }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$portableZip = Join-Path $OutDir "LitBench-$Version-x64-portable.zip"
if (Test-Path $portableZip) { Remove-Item -Force $portableZip }
Write-Host "== 便携 zip（$portableZip）"
Compress-Archive -Path $Stage -DestinationPath $portableZip -CompressionLevel Optimal
Write-Host "   完成：$([math]::Round((Get-Item $portableZip).Length/1MB,1)) MB"

if ($NsisZip -and (Test-Path $NsisZip)) {
  Write-Host "== NSIS 安装器"
  $nsisTmp = Join-Path $BuildRoot "nsis"
  Expand-Archive -Path $NsisZip -DestinationPath $nsisTmp -Force
  $makensis = Get-ChildItem $nsisTmp -Recurse -Filter "makensis.exe" | Select-Object -First 1
  if (-not $makensis) { throw "NSIS zip 里没有 makensis.exe" }
  $nsi = Join-Path $BuildRoot "LitBench.nsi"
  $installer = Join-Path $OutDir "LitBench-$Version-x64-setup.exe"
  if (Test-Path $installer) { Remove-Item -Force $installer }
  $iconLine = ""
  if (Test-Path $ico) { $iconLine = "Icon `"$ico`"" }
  $stageSizeKB = [math]::Round(((Get-ChildItem $Stage -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1KB))
  $installDate = Get-Date -Format "yyyyMMdd"
  $nsiText = @"
Unicode true
!include "MUI2.nsh"
Name "LitBench"
OutFile "$installer"
InstallDir "`$LOCALAPPDATA\LitBench"
InstallDirRegKey HKCU "Software\LitBench" "InstallDir"
RequestExecutionLevel user
SetCompressor zlib
$iconLine
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
Section "LitBench" SecMain
  SetRegView 64
  SetOutPath "`$INSTDIR"
  File /r "$Stage\*"
  WriteRegStr HKCU "Software\LitBench" "InstallDir" "`$INSTDIR"
  WriteRegStr HKCU "Software\LitBench" "Version" "$Version"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "DisplayName" "LitBench"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "DisplayVersion" "$Version"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "Publisher" "LitBench contributors"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "InstallLocation" "`$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "DisplayIcon" "`$INSTDIR\LitBench.exe,0"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "UninstallString" '"`$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "QuietUninstallString" '"`$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "EstimatedSize" $stageSizeKB
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "NoRepair" 1
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench" "InstallDate" "$installDate"
  WriteUninstaller "`$INSTDIR\uninstall.exe"
  CreateShortcut "`$DESKTOP\LitBench.lnk" "`$INSTDIR\LitBench.exe"
  CreateDirectory "`$SMPROGRAMS\LitBench"
  CreateShortcut "`$SMPROGRAMS\LitBench\LitBench.lnk" "`$INSTDIR\LitBench.exe"
  CreateShortcut "`$SMPROGRAMS\LitBench\卸载 LitBench.lnk" "`$INSTDIR\uninstall.exe"
SectionEnd
Section "Uninstall"
  SetRegView 64
  Delete "`$DESKTOP\LitBench.lnk"
  Delete "`$SMPROGRAMS\LitBench\LitBench.lnk"
  Delete "`$SMPROGRAMS\LitBench\卸载 LitBench.lnk"
  RMDir "`$SMPROGRAMS\LitBench"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\LitBench"
  DeleteRegKey HKCU "Software\LitBench"
  RMDir /r "`$INSTDIR"
SectionEnd
"@
  Set-Content -Path $nsi -Value $nsiText -Encoding UTF8
  # makensis 用 GetTempPath 建临时文件；套件环境里系统 TEMP 可能不可写，
  # 会表现为 "error creating mmap" / "!tempfile" 之类误导性错误。
  $nsisTemp = Join-Path $BuildRoot "nsis-temp"
  New-Item -ItemType Directory -Force -Path $nsisTemp | Out-Null
  $oldTemp = $env:TEMP
  $oldTmp = $env:TMP
  $env:TEMP = $nsisTemp
  $env:TMP = $nsisTemp
  try {
    & $makensis.FullName $nsi
    if ($LASTEXITCODE -ne 0) { throw "makensis 失败：$LASTEXITCODE" }
  } finally {
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
  }
  Write-Host "   完成：$([math]::Round((Get-Item $installer).Length/1MB,1)) MB"
} else {
  Write-Host "   跳过 NSIS：未提供 -NsisZip"
}

Write-Host "== 输出"
Get-ChildItem $OutDir -Filter "LitBench-$Version-x64-*" | Select-Object Name, Length
