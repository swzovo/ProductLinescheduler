#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "尚未安装 Python 依赖，请先按 README.md 执行初始化步骤。"
  exit 1
fi

if [[ ! -f "frontend/dist/index.html" ]]; then
  if [[ ! -d "frontend/node_modules" ]]; then
    echo "正在安装前端构建依赖..."
    (cd frontend && npm ci)
  fi
  echo "正在构建前端..."
  (cd frontend && npm run build)
fi

echo "产线排班系统已启动：http://127.0.0.1:8000"
echo "按 Ctrl+C 可停止服务。"
exec .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
