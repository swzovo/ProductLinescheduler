$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYINSTALLER_CONFIG_DIR = "$PSScriptRoot\build\pyinstaller-cache"

if (-not (Test-Path ".desktop-venv\Scripts\python.exe")) {
    Write-Host "首次打包：正在创建独立的桌面版构建环境..."
    py -3.12 -m venv .desktop-venv
}
& .desktop-venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
& .desktop-venv\Scripts\python.exe -c "from keyring.backends.Windows import WinVaultKeyring; assert WinVaultKeyring.priority > 0; print('Windows Credential Manager backend: OK')"

Push-Location frontend
Write-Host "正在安装已锁定的前端构建依赖..."
npm.cmd ci
npm run build
Pop-Location
& .desktop-venv\Scripts\python.exe desktop\create_icon.py

& .desktop-venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name "产线排班系统" `
    --icon "$PSScriptRoot\desktop\assets\app.ico" `
    --add-data "$PSScriptRoot\frontend\dist;frontend/dist" `
    --add-data "$PSScriptRoot\backend\assets;backend/assets" `
    --collect-all webview `
    --collect-all keyring `
    --collect-all win32ctypes `
    --collect-all cryptography `
    --collect-submodules scipy._external.array_api_compat `
    --distpath "release" `
    --workpath "build/desktop" `
    --specpath "build/desktop" `
    "$PSScriptRoot\desktop_launcher.py"

$ZipPath = "$PSScriptRoot\release\产线排班系统-Windows-x64.zip"
if (Test-Path $ZipPath) {
    Remove-Item $ZipPath -Force
}
Compress-Archive `
    -Path "$PSScriptRoot\release\产线排班系统" `
    -DestinationPath $ZipPath `
    -CompressionLevel Optimal

Write-Host "Windows 程序已生成：release\产线排班系统\产线排班系统.exe"
Write-Host "免安装分发包已生成：release\产线排班系统-Windows-x64.zip"
