#!/bin/bash
# GCP VM 部署用（Ubuntu/Debian）
set -e

echo "=== 停止舊 server ==="
pkill -f "node.*next" 2>/dev/null || true

echo "=== 更新程式碼 ==="
git pull origin master

echo "=== 安裝依賴 ==="
npm install

echo "=== Build ==="
npm run build

echo "=== 啟動 Next.js ==="
nohup npm start > /tmp/nextjs.log 2>&1 &
sleep 2

if ! ss -tlnp | grep -q ':3000'; then
  echo "=== Next.js 啟動失敗，查看 /tmp/nextjs.log ==="
  exit 1
fi
echo "=== Next.js 啟動成功（port 3000）==="

echo "=== 同步 nginx 設定 ==="
sudo cp nginx/stock.conf /etc/nginx/sites-available/stock.conf
sudo ln -sf /etc/nginx/sites-available/stock.conf /etc/nginx/sites-enabled/stock.conf
sudo nginx -t

echo "=== 重啟 nginx ==="
sudo systemctl restart nginx

echo "=== 部署完成：https://claude.venraas.tw ==="
