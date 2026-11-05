#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
LAN_IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1 || echo "127.0.0.1")
echo "=========================================================="
echo "🚀 CP-Extractor 局域网共享服务已启动！"
echo "👉 本机访问地址:   http://localhost:8000"
echo "👉 局域网同事访问: http://${LAN_IP}:8000"
echo "=========================================================="
exec ./.venv/bin/uvicorn src.cp_extractor.api.app:app --host 0.0.0.0 --port 8000 --reload
