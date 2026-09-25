#!/bin/sh
set -e

# 容器启动时确保依赖已安装（已满足时为快速 no-op）
pip install --no-cache-dir -r requirements.txt

# 启动服务；应用启动钩子会自动创建 SQLite 数据库与表结构
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
