#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# 把已经构建好的 web/dist + desktop/ 壳打包成 macOS arm64 .dmg。
#
# 运行环境：
#   - macOS：用 hdiutil，产物是标准 UDZO dmg（可签名/公证）；
#   - Linux：用 xorrisofs + libdmg-hfsplus 的 dmg 工具（交叉打包，产物未签名）；
#   - 不支持 Windows 直接运行本脚本（用 WSL/Linux 或 macOS）。
#
# 用法：
#   ./desktop/build-macos.sh \
#       --electron-zip /path/to/electron-v33.0.0-darwin-arm64.zip \
#       --out dist/LitBench-0.1.0-arm64.dmg \
#       [--renderer ../web/dist] [--version 0.1.0]
#
# 前置：web 已 `npm run build`，desktop/renderer 可由 --renderer 自动复制。
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
electron_zip=""
out=""
renderer="$repo/web/dist"
version="0.1.0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --electron-zip) electron_zip="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --renderer) renderer="$2"; shift 2 ;;
    --version) version="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [[ -z "$electron_zip" || -z "$out" ]]; then
  echo "用法：$0 --electron-zip <zip> --out <dmg> [--renderer <web/dist>] [--version x.y.z]" >&2
  exit 2
fi
if [[ ! -f "$electron_zip" ]]; then
  echo "找不到 Electron zip：$electron_zip" >&2
  exit 1
fi
if [[ ! -f "$renderer/index.html" ]]; then
  echo "找不到前端构建产物：$renderer/index.html；先在 web/ 跑 npm run build" >&2
  exit 1
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/litbench-dmg.XXXXXX")"
trap 'rm -rf "$work"' EXIT
stage="$work/stage"
app="$stage/LitBench.app"
mkdir -p "$stage"

echo "== 解压 Electron（保留 symlink）"
unzip -q "$electron_zip" -d "$work/electron"
src_app="$(find "$work/electron" -maxdepth 2 -name 'Electron.app' -type d | head -n1)"
if [[ -z "$src_app" ]]; then
  echo "Electron zip 里没有 Electron.app" >&2
  exit 1
fi
cp -a "$src_app" "$app"
rm -f "$app/Contents/Resources/default_app.asar" || true

echo "== 放入客户端代码（仅 renderer + 壳，不含 .data / 文献库 / key）"
rm -rf "$app/Contents/Resources/app"
mkdir -p "$app/Contents/Resources/app"
cp -a "$here/package.json" "$here/main.js" "$here/setup.html" "$here/setup-preload.js" \
  "$app/Contents/Resources/app/"
cp -a "$renderer" "$app/Contents/Resources/app/renderer"

if [[ -f "$here/icon.icns" ]]; then
  cp -f "$here/icon.icns" "$app/Contents/Resources/electron.icns"
fi

echo "== 写 Info.plist"
python3 - "$app/Contents/Info.plist" "$version" <<'PY'
import plistlib
import sys

path, version = sys.argv[1], sys.argv[2]
with open(path, "rb") as fh:
    data = plistlib.load(fh)
data.update({
    "CFBundleDisplayName": "LitBench",
    "CFBundleName": "LitBench",
    "CFBundleIdentifier": "com.litbench.desktop",
    "CFBundleShortVersionString": version,
    "CFBundleVersion": version,
    "NSHighResolutionCapable": True,
    "LSMinimumSystemVersion": "11.0",
    "LSApplicationCategoryType": "public.app-category.productivity",
})
with open(path, "wb") as fh:
    plistlib.dump(data, fh)
PY

echo "== 添加拖拽安装用的 Applications 快捷方式"
ln -s /Applications "$stage/Applications"

echo "== 生成 dmg（$out）"
mkdir -p "$(dirname "$out")"
rm -f "$out"
if command -v hdiutil >/dev/null 2>&1; then
  # macOS：标准 UDZO
  hdiutil create -volname "LitBench" -srcfolder "$stage" -ov -format UDZO "$out" >/dev/null
elif command -v xorrisofs >/dev/null 2>&1 && command -v dmg >/dev/null 2>&1; then
  # Linux（交叉打包）：HFS+ 文件系统 + libdmg-hfsplus 压缩
  iso="$work/LitBench.iso"
  xorrisofs -hfsplus -V "LitBench" -D -l -no-pad -r -dir-mode 0755 -o "$iso" "$stage"
  dmg "$iso" "$out"
else
  echo "缺少打包工具：macOS 需要 hdiutil；Linux 需要 xorrisofs + dmg(libdmg-hfsplus)" >&2
  exit 1
fi

if [[ ! -s "$out" ]]; then
  echo "dmg 没有生成成功" >&2
  exit 1
fi
echo "完成：$out（$(du -h "$out" | cut -f1)）"
