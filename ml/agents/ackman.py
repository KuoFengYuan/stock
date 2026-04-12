"""Bill Ackman：集中持股 + 優質成長 + 強獲利能力 + 合理估值（Quality at Reasonable Price）。"""


def ackman_agent(ctx: dict) -> dict:
    fund = ctx.get("fund", {})
    reasons = []
    score = 0

    # 獲利能力（ROE）
    roe = fund.get("roe")
    if roe is not None:
        if roe >= 20:
            score += 2; reasons.append(f"ROE {roe:.0f}% 優質")
        elif roe >= 12:
            score += 1
        elif roe < 8:
            score -= 1

    # 成長（Ackman 要求穩定成長，不要爆衝）
    ni_yoy = fund.get("ni_yoy")
    rev_yoy = fund.get("revenue_yoy")
    if ni_yoy is not None and rev_yoy is not None:
        if 10 <= ni_yoy <= 50 and 5 <= rev_yoy <= 40:
            score += 2; reasons.append("穩健雙成長")
        elif ni_yoy < -10 or rev_yoy < -10:
            score -= 2; reasons.append("獲利/營收衰退")

    # 合理估值（QARP — Quality at Reasonable Price）
    pe = fund.get("pe_ratio")
    if pe is not None and pe > 0:
        if pe <= 20:
            score += 1; reasons.append(f"PE {pe:.0f} 合理")
        elif pe > 35:
            score -= 2; reasons.append(f"PE {pe:.0f} 過高")

    # 自由現金流代理：TTM 淨利 > 0 + 負債可控
    debt = fund.get("debt_ratio")
    ni_ttm = fund.get("ni_ttm")
    if ni_ttm is not None and ni_ttm > 0 and debt is not None and debt < 50:
        score += 1; reasons.append("財務健康")
    if ni_ttm is not None and ni_ttm < 0:
        score -= 2; reasons.append("虧損")

    if score >= 3:
        return {"signal": "bullish", "confidence": min(1.0, score / 6), "reasons": reasons}
    if score <= -2:
        return {"signal": "bearish", "confidence": min(1.0, -score / 4), "reasons": reasons}
    return {"signal": "neutral", "confidence": 0.3, "reasons": reasons}
