import Database from 'better-sqlite3'
import path from 'path'
import fs from 'fs'

const DB_PATH = path.join(process.cwd(), 'data', 'stock.db')

export function runMigrations() {
  const dir = path.join(process.cwd(), 'data')
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true })

  const sqlite = new Database(DB_PATH)
  sqlite.pragma('journal_mode = WAL')
  sqlite.pragma('foreign_keys = ON')

  sqlite.exec(`
    CREATE TABLE IF NOT EXISTS stocks (
      symbol      TEXT PRIMARY KEY,
      name        TEXT NOT NULL,
      market      TEXT NOT NULL,
      industry    TEXT,
      listed_date TEXT,
      updated_at  INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS stock_prices (
      id        INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol    TEXT NOT NULL REFERENCES stocks(symbol),
      date      TEXT NOT NULL,
      open      REAL NOT NULL,
      high      REAL NOT NULL,
      low       REAL NOT NULL,
      close     REAL NOT NULL,
      volume    INTEGER NOT NULL,
      adj_close REAL,
      UNIQUE(symbol, date)
    );
    CREATE INDEX IF NOT EXISTS idx_prices_date ON stock_prices(date);

    CREATE TABLE IF NOT EXISTS financials (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol           TEXT NOT NULL,
      year             INTEGER NOT NULL,
      quarter          INTEGER NOT NULL,
      revenue          REAL,
      operating_profit REAL,
      net_income       REAL,
      eps              REAL,
      equity           REAL,
      total_assets     REAL,
      total_debt       REAL,
      UNIQUE(symbol, year, quarter)
    );

    CREATE TABLE IF NOT EXISTS recommendations (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol        TEXT NOT NULL,
      date          TEXT NOT NULL,
      score         REAL NOT NULL,
      signal        TEXT NOT NULL,
      features_json TEXT,
      reasons_json  TEXT,
      model_version TEXT,
      created_at    INTEGER NOT NULL,
      UNIQUE(symbol, date)
    );
    CREATE INDEX IF NOT EXISTS idx_rec_date_score ON recommendations(date, score DESC);

    CREATE TABLE IF NOT EXISTS settings (
      key        TEXT PRIMARY KEY,
      value      TEXT NOT NULL,
      updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS institutional (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol        TEXT NOT NULL,
      date          TEXT NOT NULL,
      foreign_net   INTEGER,
      trust_net     INTEGER,
      dealer_net    INTEGER,
      total_net     INTEGER,
      UNIQUE(symbol, date)
    );
    CREATE INDEX IF NOT EXISTS idx_inst_symbol_date ON institutional(symbol, date);

    CREATE TABLE IF NOT EXISTS margin_trading (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol          TEXT NOT NULL,
      date            TEXT NOT NULL,
      margin_buy      INTEGER,
      margin_sell     INTEGER,
      margin_balance  INTEGER,
      short_buy       INTEGER,
      short_sell      INTEGER,
      short_balance   INTEGER,
      UNIQUE(symbol, date)
    );
    CREATE INDEX IF NOT EXISTS idx_margin_symbol_date ON margin_trading(symbol, date);

    CREATE TABLE IF NOT EXISTS sync_log (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      type          TEXT NOT NULL,
      status        TEXT NOT NULL,
      records_count INTEGER,
      error_message TEXT,
      started_at    INTEGER NOT NULL,
      finished_at   INTEGER
    );

    CREATE TABLE IF NOT EXISTS monthly_revenue (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol     TEXT NOT NULL,
      year       INTEGER NOT NULL,
      month      INTEGER NOT NULL,
      revenue    REAL NOT NULL,
      yoy        REAL,
      mom        REAL,
      UNIQUE(symbol, year, month)
    );
    CREATE INDEX IF NOT EXISTS idx_monthly_rev_symbol ON monthly_revenue(symbol, year DESC, month DESC);

    CREATE TABLE IF NOT EXISTS stock_tags (
      id       INTEGER PRIMARY KEY AUTOINCREMENT,
      symbol   TEXT NOT NULL REFERENCES stocks(symbol),
      tag      TEXT NOT NULL,
      sub_tag  TEXT,
      UNIQUE(symbol, tag, sub_tag)
    );
    CREATE INDEX IF NOT EXISTS idx_stock_tags_tag ON stock_tags(tag);
    CREATE INDEX IF NOT EXISTS idx_stock_tags_symbol ON stock_tags(symbol);
  `)

  sqlite.close()
  console.log('Database migrations completed.')
}
