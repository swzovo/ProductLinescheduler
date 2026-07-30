#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PYINSTALLER_CONFIG_DIR="$PWD/build/pyinstaller-cache"

if [[ ! -x ".desktop-venv/bin/python" ]]; then
  echo "首次打包：正在创建独立的桌面版构建环境..."
  python3 -m venv .desktop-venv
fi
.desktop-venv/bin/python -m pip install -r requirements-desktop.txt

if [[ ! -d "frontend/node_modules" ]]; then
  echo "正在安装前端构建依赖..."
  (cd frontend && npm ci)
fi
(cd frontend && npm run build)
.desktop-venv/bin/python desktop/create_icon.py

.desktop-venv/bin/python -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "产线排班系统" \
  --osx-bundle-identifier "com.local.production-line-scheduler" \
  --icon "$PWD/desktop/assets/app.png" \
  --add-data "$PWD/frontend/dist:frontend/dist" \
  --add-data "$PWD/backend/assets:backend/assets" \
  --collect-all webview \
  --collect-submodules scipy._external.array_api_compat \
  --distpath "release" \
  --workpath "build/desktop" \
  --specpath "build/desktop" \
  "$PWD/desktop_launcher.py"

APP_PATH="$PWD/release/产线排班系统.app"
plutil -replace CFBundleShortVersionString -string "3.4.1" "$APP_PATH/Contents/Info.plist"
plutil -replace CFBundleVersion -string "3.4.1" "$APP_PATH/Contents/Info.plist"
codesign --force --deep --sign - "$APP_PATH"

echo ""
echo "桌面程序已生成：release/产线排班系统.app"
echo "可直接双击运行，无需再打开终端。"
