# 排程腳本

## 腳本

- `daily.sh`：每個交易日 sync + predict（約 3-5 分鐘）
- `weekly.sh`：每週 sync + train + predict（約 20 分鐘，包含重訓）

兩者 log 輸出到 `logs/` 目錄，自動清除舊 log（daily 30 天、weekly 90 天）。

## 授予執行權限

```bash
chmod +x scripts/daily.sh scripts/weekly.sh
```

## 手動測試

```bash
# 從專案根目錄
./scripts/daily.sh
./scripts/weekly.sh
```

若 conda 無法在非互動 shell 下找到，設定 `PYTHON` 環境變數指向絕對路徑：

```bash
PYTHON=/home/user/miniconda3/envs/stock/bin/python ./scripts/daily.sh
```

## cron 設定

編輯 crontab：

```bash
crontab -e
```

加入：

```cron
# 每個工作日 19:00 執行 daily pipeline（台股收盤後）
0 19 * * 1-5 cd /path/to/stock_a && ./scripts/daily.sh >> logs/cron.log 2>&1

# 每週六 12:00 執行 weekly pipeline（含重訓）
0 12 * * 6 cd /path/to/stock_a && ./scripts/weekly.sh >> logs/cron.log 2>&1
```

把 `/path/to/stock_a` 換成實際路徑（例：`/home/user/stock_a`）。

若 cron 下 conda 環境找不到，改成絕對路徑：

```cron
0 19 * * 1-5 cd /home/user/stock_a && PYTHON=/home/user/miniconda3/envs/stock/bin/python ./scripts/daily.sh
0 12 * * 6   cd /home/user/stock_a && PYTHON=/home/user/miniconda3/envs/stock/bin/python ./scripts/weekly.sh
```

## 檢查排程

```bash
# 列出
crontab -l

# 編輯
crontab -e

# 查看 cron 執行 log（依系統不同）
sudo tail -f /var/log/syslog | grep CRON     # Debian/Ubuntu
sudo journalctl -u cron -f                    # systemd

# 查看專案 log
tail -f logs/daily-*.log
tail -f logs/weekly-*.log
```

## 備註

- `daily.sh` 週末自動跳過（台股休市）
- 國定假日無法自動偵測，但 sync 會正確判別無新資料、predict 會寫入相同日期
- 建議 server 時區設為 `Asia/Taipei`：
  ```bash
  sudo timedatectl set-timezone Asia/Taipei
  ```
- log 累積空間：daily × 30 天 + weekly × 90 天，約 < 50 MB
