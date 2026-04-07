#!/bin/bash
# GCP VM 部署用（Ubuntu/Debian）
# 用法：./deploy-gcp.sh（不要加 sudo）
set -e

echo "=== 停止舊 server ==="
fuser -k 3031/tcp 2>/dev/null || true
sleep 1

echo "=== 更新程式碼 ==="
git pull origin master

echo "=== 安裝依賴 ==="
npm install

echo "=== Build ==="
rm -rf .next
npm run build

echo "=== 啟動 Next.js ==="
nohup npm start -- -p 3031 > /tmp/nextjs.log 2>&1 &
sleep 5

if ! ss -tlnp | grep -q ':3031'; then
  echo "=== Next.js 啟動失敗，查看 /tmp/nextjs.log ==="
  exit 1
fi
echo "=== Next.js 啟動成功（port 3031）==="

echo "=== 同步 nginx 設定 ==="
sudo cp nginx/stock.conf /etc/nginx/sites-available/stock.conf
sudo ln -sf /etc/nginx/sites-available/stock.conf /etc/nginx/sites-enabled/stock.conf
sudo nginx -t

echo "=== 重啟 nginx ==="
sudo systemctl restart nginx

echo "=== 部署完成：https://claude.venraas.tw ==="
