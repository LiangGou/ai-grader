#!/bin/bash
# AI 判卷一键启动脚本
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "创建虚拟环境…"
  python3 -m venv .venv
fi
source .venv/bin/activate
echo "安装依赖…"
pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  echo ""
  echo "⚠️  未检测到 .env 文件。"
  echo "   请先到「设置」页填写 OpenAI API Key，或复制 .env.example 为 .env 并填入："
  echo "   cp .env.example .env"
  echo ""
fi

echo "启动服务：http://localhost:5000"
exec flask --app app run --host=0.0.0.0 --port=5000
