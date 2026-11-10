#!/bin/bash
# ==============================================================================
# CP-Extractor 一键启动脚本 (适用于 Apple Silicon M1/M2/M3/M4 系列 macOS)
# 在 Finder 中直接双击此脚本即可自动初始化环境、启动服务并唤起浏览器
# ==============================================================================

set -e
cd "$(dirname "$0")"

echo "=========================================================="
echo "🍎 CP-Extractor | 标准→控制计划检查项提取系统"
echo "   正在为 Apple Silicon (M系列芯片) 准备运行环境..."
echo "=========================================================="

# 1. 寻找可用的 Python 3 (>= 3.10)
PYTHON_CMD=""
for cmd in python3.12 python3.11 python3; do
    if command -v $cmd &>/dev/null; then
        VER=$($cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        MAJOR=$($cmd -c "import sys; print(sys.version_info.major)")
        MINOR=$($cmd -c "import sys; print(sys.version_info.minor)")
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 10 ]; then
            PYTHON_CMD="$cmd"
            echo "✅ 找到可用 Python 版本: $cmd ($VER)"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "❌ 未检测到 Python 3.10 或更高版本！"
    echo "💡 请先在终端运行以下命令安装 Python 3.12："
    echo "   brew install python@3.12"
    echo "或访问 Python 官网下载 macOS 安装包: https://www.python.org/downloads/macos/"
    read -p "按回车键退出..."
    exit 1
fi

# 2. 检查或创建虚拟环境
if [ ! -d ".venv" ]; then
    echo "📦 首次运行，正在创建独立虚拟环境 (.venv)..."
    $PYTHON_CMD -m venv .venv
    echo "📥 正在安装必要依赖（基于 pyproject.toml）..."
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install -e .
else
    echo "✅ 虚拟环境 (.venv) 已就绪"
fi

# 3. 检查环境变量配置
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo "📝 未检测到 .env 配置文件，正在从 .env.example 自动生成..."
        cp .env.example .env
        echo "💡 提示: 您可在网页端登录系统管理员账号(admin/admin)直接配置大模型 API Key"
    fi
fi

# 4. 获取本机与局域网 IP
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1")

echo "=========================================================="
echo "🚀 正在启动服务..."
echo "👉 本机访问地址:   http://localhost:8000"
echo "👉 局域网同事访问: http://${LAN_IP}:8000"
echo "🔑 系统初始管理员: admin / admin"
echo "=========================================================="

# 5. 自动在默认浏览器中打开页面
(sleep 2 && open "http://localhost:8000") &

# 6. 启动 Uvicorn 服务
exec ./.venv/bin/uvicorn src.cp_extractor.api.app:app --host 0.0.0.0 --port 8000
