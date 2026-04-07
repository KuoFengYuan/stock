#!/bin/bash
# Mac 本機開發用
set -e

echo "=== 停止舊 server ==="
kill $(lsof -t -i:3000) 2>/dev/null || true
brew services stop nginx 2>/dev/null || true

echo "=== Build ==="
npm run build

echo "=== 啟動 Next.js ==="
nohup npm start > /tmp/nextjs.log 2>&1 &
sleep 2

if ! lsof -i :3000 | grep -q LISTEN; then
  echo "=== Next.js 啟動失敗，查看 /tmp/nextjs.log ==="
  exit 1
fi
echo "=== Next.js 啟動成功（port 3000）==="

echo "=== 同步 nginx 設定 ==="
cp nginx/stock.conf /opt/homebrew/etc/nginx/servers/stock.conf
nginx -t

echo "=== 啟動 nginx ==="
brew services start nginx
sleep 1

if lsof -i :8080 | grep -q LISTEN; then
  echo "=== nginx 啟動成功（port 8080）==="
  echo "=== 本機訪問：http://localhost:8080 ==="
else
  echo "=== nginx 啟動失敗，查看 /opt/homebrew/var/log/nginx/error.log ==="
  exit 1
fi
