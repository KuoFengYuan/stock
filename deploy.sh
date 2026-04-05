#!/bin/bash
set -e

echo "=== 停止舊 server ==="
kill $(lsof -t -i:3000) 2>/dev/null || true
pkill -f ngrok 2>/dev/null || true

echo "=== Build ==="
npm run build

echo "=== 啟動 Next.js ==="
nohup npm start > /tmp/nextjs.log 2>&1 &
sleep 2

if ! lsof -i :3000 | grep -q LISTEN; then
  echo "=== 啟動失敗，查看 /tmp/nextjs.log ==="
  exit 1
fi
echo "=== Next.js 啟動成功 ==="

echo "=== 啟動 ngrok ==="
nohup ngrok http 3000 > /tmp/ngrok.log 2>&1 &
sleep 3

URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])" 2>/dev/null)
echo "=== 公開網址：$URL ==="
