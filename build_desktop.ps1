$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYINSTALLER_CONFIG_DIR = "$PSScriptRoot\build\pyinstaller-cache"

if (-not (Test-Path ".desktop-venv\Scripts\python.exe")) {
    Write-Host "首次打包：正在创建独立的桌面版构建环境..."
    python -m venv .desktop-venv
}
& .desktop-venv\Scripts\python.exe -m pip install -r requirements-desktop.txt

Push-Location frontend
if (-not (Test-Path "node_modules")) {
    Write-Host "正在安装前端构建依赖..."
    npm.cmd ci
}
npm run build
Pop-Location

& .desktop-venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name "产线排班系统" `
    --icon "$PSScriptRoot\desktop\assets\app.png" `
    --add-data "$PSScriptRoot\frontend\dist;frontend/dist" `
    --add-data "$PSScriptRoot\backend\assets;backend/assets" `
    --collect-all webview `
    --collect-submodules scipy._external.array_api_compat `
    --distpath "release" `
    --workpath "build/desktop" `
    --specpath "build/desktop" `
    "$PSScriptRoot\desktop_launcher.py"

Write-Host "桌面程序已生成：release\产线排班系统\产线排班系统.exe"
