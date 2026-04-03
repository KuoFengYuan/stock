import { NextResponse } from 'next/server'
import { getDb } from '@/lib/db'
import fs from 'fs'
import path from 'path'

const MODEL_PATH = path.join(process.cwd(), 'ml', 'model.pkl')

// 建議重訓門檻：距上次訓練後新增超過此筆數的價格資料
const RETRAIN_THRESHOLD = 5000

export async function GET() {
  const db = getDb()

  // 模型檔案狀態
  const modelExists = fs.existsSync(MODEL_PATH)
  const modelMtime = modelExists ? fs.statSync(MODEL_PATH).mtimeMs : null

  // 上次訓練紀錄
  const lastTrain = db.prepare(
    `SELECT started_at, finished_at, records_count FROM sync_log WHERE type = 'train' AND status = 'success' ORDER BY finished_at DESC LIMIT 1`
  ).get() as { started_at: number; finished_at: number; records_count: number } | undefined

  // 上次訓練後新增的價格筆數
  let newPricesSinceTrain = 0
  if (lastTrain?.finished_at) {
    const trainDate = new Date(lastTrain.finished_at).toISOString().slice(0, 10)
    const row = db.prepare(
      `SELECT COUNT(*) as cnt FROM stock_prices WHERE date > ?`
    ).get(trainDate) as { cnt: number }
    newPricesSinceTrain = row.cnt
  } else {
    // 從未訓練過：統計全部資料量
    const row = db.prepare(`SELECT COUNT(*) as cnt FROM stock_prices`).get() as { cnt: number }
    newPricesSinceTrain = row.cnt
  }

  const shouldRetrain = newPricesSinceTrain >= RETRAIN_THRESHOLD

  // 總資料量統計
  const priceCount = (db.prepare(`SELECT COUNT(*) as cnt FROM stock_prices`).get() as { cnt: number }).cnt
  const symbolCount = (db.prepare(`SELECT COUNT(*) as cnt FROM stocks`).get() as { cnt: number }).cnt

  return NextResponse.json({
    modelExists,
    modelMtime,
    lastTrainAt: lastTrain?.finished_at ?? null,
    newPricesSinceTrain,
    shouldRetrain,
    retrainThreshold: RETRAIN_THRESHOLD,
    priceCount,
    symbolCount,
  })
}
