#!/usr/bin/env bash
# 每週排程：sync all + train + predict
# 建議時機：週六中午（有完整一週新資料）

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
TS="$(date +'%Y%m%d-%H%M%S')"
LOG="$LOG_DIR/weekly-$TS.log"

log() {
    local msg="[$(date +'%Y-%m-%d %H:%M:%S')] $*"
    echo "$msg"
    echo "$msg" >> "$LOG"
}

log "=== Weekly pipeline 開始（含重訓）==="

PYTHON="${PYTHON:-$(conda run -n stock which python 2>/dev/null || echo python3)}"

log "Step 1/3: sync all"
"$PYTHON" ml/sync.py all >> "$LOG" 2>&1
[ $? -ne 0 ] && { log "sync 失敗"; exit 1; }

log "Step 2/3: train（4 模型，約 15 分鐘）"
"$PYTHON" ml/train.py >> "$LOG" 2>&1
[ $? -ne 0 ] && { log "train 失敗"; exit 1; }

log "Step 3/3: predict"
"$PYTHON" ml/predict.py >> "$LOG" 2>&1
[ $? -ne 0 ] && { log "predict 失敗"; exit 1; }

log "=== Weekly pipeline 完成 ==="

# 保留 90 天 weekly log
find "$LOG_DIR" -name 'weekly-*.log' -mtime +90 -delete 2>/dev/null || true
