import Database from 'better-sqlite3'
import path from 'path'
import fs from 'fs'

const DB_PATH = path.join(process.cwd(), 'data', 'stock.db')

let _db: Database.Database | null = null

export function getDb(): Database.Database {
  if (!_db) {
    const dir = path.join(process.cwd(), 'data')
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true })
    _db = new Database(DB_PATH)
    _db.pragma('journal_mode = WAL')
    _db.pragma('busy_timeout = 30000')
    _db.pragma('foreign_keys = ON')
  }
  return _db
}
