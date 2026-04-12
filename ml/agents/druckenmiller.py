"""Stan Druckenmiller：趨勢動能 + 相對強度 + 不抗勢。"""


def druckenmiller_agent(ctx: dict) -> dict:
    tech = ctx.get("tech", {})
    fund = ctx.get("fund", {})
    minervini = ctx.get("minervini", {}) or {}
    rs_pctile = ctx.get("rs_pctile")
    reasons = []
    score = 0

    # RS 排名（Druckenmiller 重視相對強度）
    if rs_pctile is not None:
        if rs_pctile >= 85:
            score += 3; reasons.append(f"RS 前 {100-rs_pctile:.0f}%")
        elif rs_pctile >= 70:
            score += 2; reasons.append(f"RS {rs_pctile:.0f}")
        elif rs_pctile < 30:
            score -= 2; reasons.append(f"RS {rs_pctile:.0f} 弱勢")

    # Minervini 趨勢模板
    if minervini.get("minervini"):
        score += 2; reasons.append("Minervini 趨勢")

    # 短期動能
    return20 = tech.get("return20d")
    if return20 is not None:
        if return20 > 10:
            score += 1; reasons.append(f"20日 +{return20:.0f}%")
        elif return20 < -15:
            score -= 2; reasons.append(f"20日 {return20:.0f}% 破位")

    # 避免接刀
    sma20_bias = tech.get("sma20_bias")
    if sma20_bias is not None and sma20_bias < -0.1:
        score -= 1; reasons.append("跌破月線 10%")

    # 不碰爛基本面（Druckenmiller 不是純技術派）
    ni_ttm = fund.get("ni_ttm")
    if ni_ttm is not None and ni_ttm < 0:
        score -= 1

    if score >= 3:
        return {"signal": "bullish", "confidence": min(1.0, score / 6), "reasons": reasons}
    if score <= -2:
        return {"signal": "bearish", "confidence": min(1.0, -score / 4), "reasons": reasons}
    return {"signal": "neutral", "confidence": 0.3, "reasons": reasons}
