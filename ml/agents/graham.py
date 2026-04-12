"""Benjamin Graham：深度價值，低 PE + 低 PB + 安全邊際 + 股息。"""


def graham_agent(ctx: dict) -> dict:
    fund = ctx.get("fund", {})
    reasons = []
    score = 0

    pe = fund.get("pe_ratio")
    pb = fund.get("pb_ratio")

    if pe is not None and pe > 0:
        if pe <= 15:
            score += 2; reasons.append(f"PE {pe:.0f} 低估")
        elif pe <= 20:
            score += 1
        elif pe > 25:
            score -= 1; reasons.append(f"PE {pe:.0f} 昂貴")

    if pb is not None and pb > 0:
        if pb <= 1.5:
            score += 2; reasons.append(f"PB {pb:.1f} 低估")
        elif pb <= 3:
            score += 1
        elif pb > 5:
            score -= 2; reasons.append(f"PB {pb:.1f} 過高")

    # Graham Number: sqrt(22.5 * EPS * BVPS)
    if pe is not None and pb is not None and pe > 0 and pb > 0:
        combined = pe * pb
        if combined < 22.5:
            score += 1; reasons.append("符合 Graham Number")

    div = fund.get("div_yield")
    if div is not None:
        if div >= 4:
            score += 1; reasons.append(f"殖利率 {div:.1f}%")
        elif div < 1:
            score -= 1

    debt = fund.get("debt_ratio")
    if debt is not None and debt > 60:
        score -= 1; reasons.append(f"負債 {debt:.0f}% 過高")

    ni_ttm = fund.get("ni_ttm")
    if ni_ttm is not None and ni_ttm < 0:
        # 虧損直接否決 — Graham 絕對不碰虧損股
        return {"signal": "bearish", "confidence": 0.9, "reasons": ["虧損（Graham 不碰）"]}

    if score >= 3:
        return {"signal": "bullish", "confidence": min(1.0, score / 6), "reasons": reasons}
    if score <= -2:
        return {"signal": "bearish", "confidence": min(1.0, -score / 4), "reasons": reasons}
    return {"signal": "neutral", "confidence": 0.3, "reasons": reasons}
