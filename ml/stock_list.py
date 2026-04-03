"""
動態取得全市場股票清單（上市 + 上櫃，只取普通股）
"""
import requests
import json
import warnings
from datetime import date, timedelta

warnings.filterwarnings("ignore")

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
_SESSION.verify = False


def _recent_weekday() -> str:
    """取最近的工作日（YYYYMMDD）"""
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def _tpex_date(d: str) -> str:
    """YYYYMMDD -> 民國年 YYY/MM/DD"""
    y, m, day = int(d[:4]) - 1911, d[4:6], d[6:]
    return f"{y}/{m}/{day}"


def fetch_all_stocks() -> tuple[list[str], dict[str, str]]:
    """
    回傳 (symbols, names)
    symbols: ['2330.TW', '2317.TW', ...]
    names:   {'2330.TW': '台積電', ...}
    """
    date_str = _recent_weekday()
    symbols = []
    names = {}

    # ── 上市 ──
    try:
        url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json&date={date_str}"
        r = _SESSION.get(url, timeout=15)
        data = r.json()
        for row in data.get("data", []):
            code = row[0].strip()
            name = row[1].strip()
            # 只要4碼數字的普通股，排除 ETF/ETN（名稱含特定字）
            if len(code) == 4 and code.isdigit():
                sym = f"{code}.TW"
                symbols.append(sym)
                names[sym] = name
    except Exception as e:
        print(f"[WARN] 上市清單取得失敗: {e}", flush=True)

    # ── 上櫃 ──
    try:
        url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&o=json&d={_tpex_date(date_str)}"
        r = _SESSION.get(url, timeout=15)
        data = json.loads(r.content.decode("utf-8"))
        tables = data.get("tables", [])
        for row in (tables[0].get("data", []) if tables else []):
            code = row[0].strip()
            name = row[1].strip()
            if len(code) == 4 and code.isdigit():
                sym = f"{code}.TWO"
                symbols.append(sym)
                names[sym] = name
    except Exception as e:
        print(f"[WARN] 上櫃清單取得失敗: {e}", flush=True)

    return symbols, names


# 靜態 fallback（原 150 檔，當 API 不可用時使用）
_FALLBACK = [
    "2330.TW","2317.TW","2454.TW","2308.TW","2303.TW","2412.TW","2882.TW","2881.TW",
    "1301.TW","2886.TW","2884.TW","2891.TW","2357.TW","3711.TW","2885.TW","2892.TW",
    "5880.TW","2207.TW","1303.TW","2883.TW","1326.TW","2880.TW","2002.TW","2887.TW",
    "6505.TW","3045.TW","2379.TW","2382.TW","2395.TW","2408.TW","2327.TW","2301.TW",
    "2344.TW","2353.TW","2356.TW","2360.TW","2376.TW","2385.TW","2404.TW","2409.TW",
    "2448.TW","2449.TW","2474.TW","2492.TW","3008.TW","3034.TW","3037.TW","3231.TW",
    "4938.TW","6415.TW",
]

try:
    ALL_STOCKS, _DYNAMIC_NAMES = fetch_all_stocks()
    if len(ALL_STOCKS) < 100:
        raise ValueError("清單過短，使用 fallback")
except Exception:
    ALL_STOCKS = _FALLBACK
    _DYNAMIC_NAMES = {}
