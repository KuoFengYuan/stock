#!/usr/bin/env bash
# 每日排程：sync all + predict
# 建議時機：台股交易日 19:00（收盤後資料齊全）
# cron 設定見 scripts/README.md

set -uo pipefail

# 切到專案根目錄
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
TS="$(date +'%Y%m%d-%H%M%S')"
LOG="$LOG_DIR/daily-$TS.log"

log() {
    local msg="[$(date +'%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG"
}

# 週末跳過（台股休市）
dow=$(date +%u)  # 1=Mon, 7=Sun
if [ "$dow" -ge 6 ]; then
    log "週末跳過排程"
    exit 0
fi

log "=== Daily pipeline 開始 ==="

# 使用 conda 環境（若 conda 在非登入 shell 下無法使用，直接用絕對路徑）
PYTHON="${PYTHON:-$(conda run -n stock which python 2>/dev/null || echo python3)}"

log "Step 1/2: sync all"
"$PYTHON" ml/sync.py all >> "$LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
    log "sync 失敗 exit=$rc"
    exit 1
fi

log "Step 2/2: predict"
"$PYTHON" ml/predict.py >> "$LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
    log "predict 失敗 exit=$rc"
    exit 1
fi

log "=== Daily pipeline 完成 ==="

# 清 30 天前的舊 log
find "$LOG_DIR" -name 'daily-*.log' -mtime +30 -delete 2>/dev/null || true
