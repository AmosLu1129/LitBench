#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# 打包「个人版」macOS arm64 客户端：Electron 壳 + 内嵌 Python 服务端 + pandoc/typst。
#
# 运行环境：
#   - macOS：用系统 hdiutil 产标准 dmg；
#   - Linux/WSL：用 xorrisofs + libdmg-hfsplus 的 dmg 交叉打包（产物未签名）。
#
# 用法（在仓库根目录）：
#   ./desktop/build-macos-embedded.sh \
#     --electron-zip /path/electron-v33.0.0-darwin-arm64.zip \
#     --python-tar /path/cpython-3.12.16+...-aarch64-apple-darwin-install_only.tar.gz \
#     --wheels-dir /path/macos-arm64-wheels \
#     --pandoc-zip /path/pandoc-3.14-arm64-macOS.zip \
#     --typst-tar /path/typst-aarch64-apple-darwin.tar.xz \
#     --out dist/LitBench-0.1.0-arm64.dmg
#
# 前置：web/ 已 npm run build；wheels 用 pip download --platform macosx_*_arm64 准备。
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
electron_zip=""
python_tar=""
wheels_dir=""
pandoc_zip=""
typst_tar=""
out=""
renderer="$repo/web/dist"
version="0.1.0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --electron-zip) electron_zip="$2"; shift 2 ;;
    --python-tar) python_tar="$2"; shift 2 ;;
    --wheels-dir) wheels_dir="$2"; shift 2 ;;
    --pandoc-zip) pandoc_zip="$2"; shift 2 ;;
    --typst-tar) typst_tar="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --renderer) renderer="$2"; shift 2 ;;
    --version) version="$2"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

for required in electron_zip python_tar wheels_dir pandoc_zip typst_tar out; do
  if [[ -z "${!required}" ]]; then
    echo "缺少 --${required//_/-}" >&2
    exit 2
  fi
done
[[ -f "$electron_zip" ]] || { echo "找不到 Electron zip：$electron_zip" >&2; exit 1; }
[[ -f "$python_tar" ]] || { echo "找不到 Python tar：$python_tar" >&2; exit 1; }
[[ -d "$wheels_dir" ]] || { echo "找不到 wheels 目录：$wheels_dir" >&2; exit 1; }
[[ -f "$pandoc_zip" ]] || { echo "找不到 pandoc zip：$pandoc_zip" >&2; exit 1; }
[[ -f "$typst_tar" ]] || { echo "找不到 typst tar：$typst_tar" >&2; exit 1; }
[[ -f "$renderer/index.html" ]] || { echo "找不到前端构建产物：$renderer/index.html" >&2; exit 1; }

work="$(mktemp -d "${TMPDIR:-/tmp}/litbench-embed.XXXXXX")"
trap 'rm -rf "$work"' EXIT
stage="$work/stage"
app="$stage/LitBench.app"
resources="$app/Contents/Resources"
backend="$resources/backend"
mkdir -p "$stage"

echo "== Electron"
unzip -q "$electron_zip" -d "$work/electron"
src_app="$(find "$work/electron" -maxdepth 2 -name 'Electron.app' -type d | head -n1)"
[[ -n "$src_app" ]] || { echo "Electron zip 里没有 Electron.app" >&2; exit 1; }
cp -a "$src_app" "$app"
mkdir -p "$backend"
rm -f "$resources/default_app.asar" || true

echo "== 客户端壳 + 前端"
rm -rf "$resources/app"
mkdir -p "$resources/app"
cp -a "$here/package.json" "$here/main.js" "$here/setup.html" "$here/setup-preload.js" \
  "$resources/app/"
cp -a "$renderer" "$resources/app/renderer"

echo "== Python 运行时"
mkdir -p "$work/py"
tar -xzf "$python_tar" -C "$work/py"
py_root="$(find "$work/py" -maxdepth 2 -type d -name python -print -quit)"
if [[ -z "$py_root" ]]; then
  # install_only 包有的版本直接解出 bin/ lib/
  if [[ -d "$work/py/bin" ]]; then py_root="$work/py"; else
    echo "Python tar 结构不认识" >&2; exit 1
  fi
fi
cp -a "$py_root" "$backend/python"
site="$backend/python/lib/python3.12/site-packages"
mkdir -p "$site"
echo "== wheels → site-packages（$(ls "$wheels_dir"/*.whl 2>/dev/null | wc -l) 个）"
for wheel in "$wheels_dir"/*.whl; do
  unzip -q "$wheel" -d "$site"
done

echo "== 后端源码"
tar -C "$repo/server" -cf - --exclude='__pycache__' litbench | tar -C "$backend" -xf -
cp -a "$here/bootstrap.py" "$backend/bootstrap.py"

echo "== pandoc / typst"
mkdir -p "$work/pandoc" "$work/typst" "$backend/bin"
unzip -q "$pandoc_zip" -d "$work/pandoc"
pandoc_bin="$(find "$work/pandoc" -type f -name pandoc | head -n1)"
[[ -n "$pandoc_bin" ]] || { echo "pandoc zip 里没有 pandoc" >&2; exit 1; }
cp -a "$pandoc_bin" "$backend/bin/pandoc"
tar -xJf "$typst_tar" -C "$work/typst"
typst_bin="$(find "$work/typst" -type f -name typst | head -n1)"
[[ -n "$typst_bin" ]] || { echo "typst tar 里没有 typst" >&2; exit 1; }
cp -a "$typst_bin" "$backend/bin/typst"
chmod +x "$backend/bin/pandoc" "$backend/bin/typst"
chmod +x "$backend/python/bin/"python3* 2>/dev/null || true

echo "== 许可证/第三方声明"
cp -a "$repo/LICENSE" "$resources/LICENSE" 2>/dev/null || true
cp -a "$repo/LICENSING.md" "$resources/LICENSING.md" 2>/dev/null || true
cp -a "$repo/THIRD_PARTY.md" "$resources/THIRD_PARTY.md" 2>/dev/null || true

echo "== Info.plist / 图标"
python3 - "$app/Contents/Info.plist" "$version" <<'PY'
import plistlib
import sys

path, version = sys.argv[1], sys.argv[2]
with open(path, "rb") as fh:
    data = plistlib.load(fh)
data.update({
    "CFBundleDisplayName": "LitBench",
    "CFBundleName": "LitBench",
    "CFBundleIdentifier": "com.litbench.desktop.embedded",
    "CFBundleShortVersionString": version,
    "CFBundleVersion": version,
    "NSHighResolutionCapable": True,
    # 内嵌的 pypdfium2 / opencv wheel 最低 macOS 13
    "LSMinimumSystemVersion": "13.0",
    "LSApplicationCategoryType": "public.app-category.productivity",
})
with open(path, "wb") as fh:
    plistlib.dump(data, fh)
PY
if [[ -f "$here/icon.icns" ]]; then
  cp -f "$here/icon.icns" "$resources/electron.icns"
fi

echo "== Applications 快捷方式"
ln -s /Applications "$stage/Applications"

echo "== 生成 dmg（$out）"
mkdir -p "$(dirname "$out")"
rm -f "$out"
if command -v hdiutil >/dev/null 2>&1; then
  hdiutil create -volname "LitBench" -srcfolder "$stage" -ov -format UDZO "$out" >/dev/null
elif command -v xorrisofs >/dev/null 2>&1 && command -v dmg >/dev/null 2>&1; then
  iso="$work/LitBench.iso"
  xorrisofs -hfsplus -V "LitBench" -D -l -no-pad -r -dir-mode 0755 -o "$iso" "$stage"
  dmg "$iso" "$out"
else
  echo "缺少打包工具：macOS 需要 hdiutil；Linux 需要 xorrisofs + dmg(libdmg-hfsplus)" >&2
  exit 1
fi
[[ -s "$out" ]] || { echo "dmg 没有生成成功" >&2; exit 1; }
echo "完成：$out（$(du -h "$out" | cut -f1)）"
