"""Warren Buffett：高 ROE + 低負債 + 穩定獲利 + 合理估值 + 護城河。"""


def buffett_agent(ctx: dict) -> dict:
    fund = ctx.get("fund", {})
    reasons = []
    score = 0  # -3 ~ +5

    roe = fund.get("roe")
    if roe is not None:
        if roe >= 20:
            score += 2; reasons.append(f"ROE {roe:.0f}% 優異")
        elif roe >= 15:
            score += 1; reasons.append(f"ROE {roe:.0f}% 良好")
        elif roe < 8:
            score -= 1; reasons.append(f"ROE {roe:.0f}% 偏低")

    debt = fund.get("debt_ratio")
    if debt is not None:
        if debt < 40:
            score += 1; reasons.append(f"負債 {debt:.0f}% 穩健")
        elif debt > 60:
            score -= 1; reasons.append(f"負債 {debt:.0f}% 偏高")

    ni_yoy = fund.get("ni_yoy")
    if ni_yoy is not None and ni_yoy > 10:
        score += 1; reasons.append(f"獲利成長 {ni_yoy:.0f}%")

    pe = fund.get("pe_ratio")
    if pe is not None and pe > 0:
        if pe <= 18:
            score += 1; reasons.append(f"PE {pe:.0f} 合理")
        elif pe > 30:
            score -= 1; reasons.append(f"PE {pe:.0f} 過高")

    ni_ttm = fund.get("ni_ttm")
    if ni_ttm is not None and ni_ttm < 0:
        score -= 2; reasons.append("虧損")

    if score >= 3:
        return {"signal": "bullish", "confidence": min(1.0, score / 5), "reasons": reasons}
    if score <= -1:
        return {"signal": "bearish", "confidence": min(1.0, -score / 3), "reasons": reasons}
    return {"signal": "neutral", "confidence": 0.3, "reasons": reasons}
