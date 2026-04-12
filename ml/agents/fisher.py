"""Philip Fisher：高成長 + 基本面扎實 + 長期趨勢（營收 / 獲利連續成長）。"""


def fisher_agent(ctx: dict) -> dict:
    fund = ctx.get("fund", {})
    monthly = ctx.get("monthly", {})
    reasons = []
    score = 0

    rev_yoy = fund.get("revenue_yoy")
    if rev_yoy is not None:
        if rev_yoy >= 25:
            score += 2; reasons.append(f"營收 YoY +{rev_yoy:.0f}% 強勁")
        elif rev_yoy >= 10:
            score += 1; reasons.append(f"營收 YoY +{rev_yoy:.0f}%")
        elif rev_yoy < -5:
            score -= 2; reasons.append(f"營收衰退 {rev_yoy:.0f}%")

    ni_yoy = fund.get("ni_yoy")
    if ni_yoy is not None:
        if ni_yoy >= 30:
            score += 2; reasons.append(f"獲利 YoY +{ni_yoy:.0f}% 爆發")
        elif ni_yoy >= 15:
            score += 1
        elif ni_yoy < -15:
            score -= 2; reasons.append(f"獲利衰退 {ni_yoy:.0f}%")

    # 月營收連續成長
    rev_yoy_months = monthly.get("rev_consecutive_yoy")
    if rev_yoy_months is not None:
        if rev_yoy_months >= 6:
            score += 1; reasons.append(f"月營收連 {rev_yoy_months} 月年增")
    rev_accel = monthly.get("rev_accel")
    if rev_accel:
        score += 1; reasons.append("月營收加速成長")

    roe = fund.get("roe")
    if roe is not None and roe >= 15:
        score += 1

    ni_ttm = fund.get("ni_ttm")
    if ni_ttm is not None and ni_ttm < 0:
        score -= 2

    if score >= 3:
        return {"signal": "bullish", "confidence": min(1.0, score / 6), "reasons": reasons}
    if score <= -2:
        return {"signal": "bearish", "confidence": min(1.0, -score / 4), "reasons": reasons}
    return {"signal": "neutral", "confidence": 0.3, "reasons": reasons}
