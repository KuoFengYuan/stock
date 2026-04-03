"""
華爾街選股策略（從 rule_engine.py 拆出）。
- Piotroski F-Score（品質因子）
- PEG Ratio（Peter Lynch 成長價值）
- Minervini SEPA 趨勢模板
"""


def calc_piotroski(symbol: str, conn) -> dict:
    """
    Piotroski F-Score（6/9，缺現金流資料）。
    每項 0 或 1，滿分 6。用於區分「真價值」vs「價值陷阱」。
    """
    rows = conn.execute(
        """SELECT year, quarter, revenue, operating_profit, net_income, eps,
                  equity, total_assets, total_debt
           FROM financials WHERE symbol=? ORDER BY year DESC, quarter DESC LIMIT 8""",
        (symbol,)
    ).fetchall()

    if len(rows) < 5:
        return {}

    score = 0
    details = []

    cur = rows[:4]
    prev = rows[4:8] if len(rows) >= 8 else rows[4:]

    def _sum(rows_list, col):
        return sum(r[col] for r in rows_list if r[col] is not None)

    def _safe_div(a, b):
        return a / b if b and b != 0 else None

    ni_ttm = _sum(cur, "net_income")
    ta_cur = cur[0]["total_assets"]
    ta_prev = prev[0]["total_assets"] if prev else None

    # 1. ROA > 0
    roa = _safe_div(ni_ttm, ta_cur)
    if roa is not None and roa > 0:
        score += 1
        details.append("ROA>0")

    # 2. Delta ROA > 0
    if prev and ta_prev:
        roa_prev = _safe_div(_sum(prev, "net_income"), ta_prev)
        if roa is not None and roa_prev is not None and roa > roa_prev:
            score += 1
            details.append("ROA+")

    # 3. Delta leverage < 0
    debt_cur = _safe_div(cur[0]["total_debt"], ta_cur)
    if prev and ta_prev:
        debt_prev = _safe_div(prev[0]["total_debt"], ta_prev)
        if debt_cur is not None and debt_prev is not None and debt_cur < debt_prev:
            score += 1
            details.append("Debt-")

    # 4. No new shares
    shares_cur = _safe_div(cur[0]["net_income"], cur[0]["eps"]) if cur[0]["eps"] and cur[0]["eps"] != 0 else None
    shares_prev = _safe_div(prev[0]["net_income"], prev[0]["eps"]) if prev and prev[0]["eps"] and prev[0]["eps"] != 0 else None
    if shares_cur and shares_prev and shares_cur <= shares_prev * 1.02:
        score += 1
        details.append("NoDiv")

    # 5. Delta gross margin > 0
    opm_cur = _safe_div(_sum(cur, "operating_profit"), _sum(cur, "revenue"))
    if prev:
        opm_prev = _safe_div(_sum(prev, "operating_profit"), _sum(prev, "revenue"))
        if opm_cur is not None and opm_prev is not None and opm_cur > opm_prev:
            score += 1
            details.append("OPM+")

    # 6. Delta asset turnover > 0
    at_cur = _safe_div(_sum(cur, "revenue"), ta_cur)
    if prev and ta_prev:
        at_prev = _safe_div(_sum(prev, "revenue"), ta_prev)
        if at_cur is not None and at_prev is not None and at_cur > at_prev:
            score += 1
            details.append("AT+")

    return {"piotroski": score, "piotroski_details": details}


def calc_peg(fund: dict) -> dict:
    """PEG = PE / EPS 成長率。Lynch 認為 PEG < 1 是低估。"""
    pe = fund.get("pe_ratio")
    eps_ttm = fund.get("eps_ttm")
    eps_ttm_prev = fund.get("eps_ttm_prev")

    if not pe or pe <= 0 or not eps_ttm or not eps_ttm_prev:
        return {}
    if eps_ttm_prev <= 0:
        return {}

    eps_growth = (eps_ttm - eps_ttm_prev) / eps_ttm_prev * 100
    if eps_growth < 5:
        return {}

    peg = pe / eps_growth
    return {"peg": round(peg, 2), "eps_growth": round(eps_growth, 1)}


def calc_minervini(tech: dict, close: float) -> dict:
    """
    Minervini SEPA 趨勢模板（8 項條件）。
    通過越多項 -> 趨勢越健康。
    """
    sma50 = tech.get("sma50")
    sma150 = tech.get("sma150")
    sma200 = tech.get("sma200")
    sma200_1m = tech.get("sma200_1m_ago")
    high_1y = tech.get("high_1y")
    low_1y = tech.get("low_1y")

    if not all([sma50, sma150, sma200, close]):
        return {}

    score = 0
    details = []

    if close > sma150:
        score += 1; details.append("P>150")
    if close > sma200:
        score += 1; details.append("P>200")
    if sma150 > sma200:
        score += 1; details.append("150>200")
    if sma200_1m and sma200 > sma200_1m:
        score += 1; details.append("200up")
    if sma50 > sma150:
        score += 1; details.append("50>150")
    if close > sma50:
        score += 1; details.append("P>50")
    if low_1y and low_1y > 0 and close >= low_1y * 1.25:
        score += 1; details.append(">25%Low")
    if high_1y and high_1y > 0 and close >= high_1y * 0.75:
        score += 1; details.append("<25%Hi")

    return {"minervini": score, "minervini_details": details}
