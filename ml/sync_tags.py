"""
同步 stock_tags 表：讀取 data/ai_tags.json 並寫入 DB
用法：python ml/sync_tags.py
"""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "stock.db"
TAGS_PATH = Path(__file__).parent.parent / "data" / "ai_tags.json"


def sync_tags():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    # 建表（若不存在）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_tags (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol   TEXT NOT NULL,
            tag      TEXT NOT NULL,
            sub_tag  TEXT,
            UNIQUE(symbol, tag, sub_tag)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_tags_tag ON stock_tags(tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_tags_symbol ON stock_tags(symbol)")

    # 清除舊資料重建
    conn.execute("DELETE FROM stock_tags")

    with open(TAGS_PATH, encoding="utf-8") as f:
        data = json.load(f)

    # 取得 DB 中所有 symbol
    existing = set(
        r[0] for r in conn.execute("SELECT symbol FROM stocks").fetchall()
    )

    count = 0
    for entry in data["tags"]:
        tag = entry["tag"]
        sub_tag = entry.get("sub_tag")
        for code in entry["symbols"]:
            # 嘗試 .TW 和 .TWO
            symbol = None
            if f"{code}.TW" in existing:
                symbol = f"{code}.TW"
            elif f"{code}.TWO" in existing:
                symbol = f"{code}.TWO"
            if not symbol:
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO stock_tags (symbol, tag, sub_tag) VALUES (?, ?, ?)",
                    (symbol, tag, sub_tag),
                )
                count += 1
            except Exception as e:
                print(f"  [WARN] {symbol}: {e}")

    conn.commit()
    conn.close()
    print(f"標籤同步完成，共 {count} 筆")


if __name__ == "__main__":
    sync_tags()
