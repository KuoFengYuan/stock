#!/bin/bash
# 本機開發用（Next.js dev mode，不需要 nginx）
set -e

echo "=== 停止舊 server ==="
lsof -ti:3000 | xargs kill -9 2>/dev/null || true
sleep 1

echo "=== 啟動 Next.js dev server ==="
npm run dev
