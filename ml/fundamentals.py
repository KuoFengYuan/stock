"""
基本面計算（單一來源）。
rule_engine.py / features.py / predict.py 共用，避免重複邏輯。
"""
import numpy as np


def _estimate_shares(rows, n=4) -> float | None:
    """用最近 n 季 NI/EPS 推算股數中位數，減少配股/庫藏股雜訊。"""
    candidates = []
    for r in rows[:n]:
        eps = r["eps"] if isinstance(r, dict) else r.get("eps") if hasattr(r, "get") else r["eps"]
        ni = r["net_income"] if isinstance(r, dict) else r.get("net_income") if hasattr(r, "get") else r["net_income"]
        if eps and eps != 0 and ni:
            s = ni / eps
            if s > 0:
                candidates.append(s)
    return float(np.median(candidates)) if candidates else None


def calc_fundamentals(symbol: str, conn, price: float | None = None) -> dict:
    """
    計算基本面指標（合併原 calc_fundamentals + calc_pe_pb + _get_fund_features）。
    傳入 price 時同時計算 PE/PB。
    """
    rows = conn.execute(
        """SELECT year, quarter, revenue, operating_profit, net_income, eps,
                  equity, total_assets, total_debt
           FROM financials WHERE symbol=? ORDER BY year DESC, quarter DESC LIMIT 8""",
        (symbol,)
    ).fetchall()

    if not rows:
        return {}

    # 轉成 dict list 以兼容 sqlite3.Row 和 dict
    rows = [dict(r) for r in rows]
    result = {}
    latest = rows[0]

    shares = _estimate_shares(rows)

    # EPS TTM
    eps_parts = []
    for r in rows[:4]:
        if r.get("eps") is not None:
            eps_parts.append(r["eps"])
        elif r.get("net_income") is not None and shares and shares > 0:
            eps_parts.append(r["net_income"] / shares)
    if eps_parts:
        result["eps_ttm"] = sum(eps_parts)

    # 去年同期 EPS TTM（PEG 計算用）
    if len(rows) >= 8:
        eps_prev = []
        for r in rows[4:8]:
            if r.get("eps") is not None:
                eps_prev.append(r["eps"])
            elif r.get("net_income") is not None and shares and shares > 0:
                eps_prev.append(r["net_income"] / shares)
        if eps_prev:
            result["eps_ttm_prev"] = sum(eps_prev)

    # TTM 淨利
    ni_list = [r["net_income"] for r in rows[:4] if r.get("net_income") is not None]
    if len(ni_list) >= 2:
        result["ni_ttm"] = sum(ni_list)

    # Equity
    equity_cur = latest.get("equity")
    equity_prev = rows[1].get("equity") if len(rows) > 1 else None
    if equity_cur and equity_cur > 0:
        result["equity"] = equity_cur

    # ROE（TTM 淨利 / 平均 equity）
    if ni_list and equity_cur and equity_cur > 0:
        avg_eq = (equity_cur + equity_prev) / 2 if equity_prev and equity_prev > 0 else equity_cur
        result["roe"] = sum(ni_list) / avg_eq * 100

    # 負債比
    ta = latest.get("total_assets")
    td = latest.get("total_debt")
    if ta and ta > 0 and td is not None:
        result["debt_ratio"] = td / ta * 100

    # 營收
    if latest.get("revenue"):
        result["revenue_abs"] = latest["revenue"]
    if len(rows) >= 5 and rows[0].get("revenue") and rows[4].get("revenue") and rows[4]["revenue"] > 0:
        result["revenue_yoy"] = (rows[0]["revenue"] - rows[4]["revenue"]) / rows[4]["revenue"] * 100

    # 淨利 YoY
    if len(rows) >= 5 and rows[0].get("net_income") and rows[4].get("net_income"):
        base_ni = rows[0]["net_income"]
        prev_ni = rows[4]["net_income"]
        if prev_ni > 0 and prev_ni > abs(base_ni) * 0.05:
            yoy = (base_ni - prev_ni) / prev_ni * 100
            if yoy <= 500:
                result["ni_yoy"] = yoy

    # PE / PB（需要股價）
    if price and price > 0:
        eps_ttm = result.get("eps_ttm")
        if eps_ttm and eps_ttm > 0:
            result["pe_ratio"] = price / eps_ttm
        if shares and shares > 0 and equity_cur and equity_cur > 0:
            bvps = equity_cur / shares
            if bvps > 0:
                result["pb_ratio"] = price / bvps

    return result
