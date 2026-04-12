"""Charlie Munger：高 ROE 複利機器 + 低負債 + 產業優勢（用 AI 標籤近似護城河）+ 合理價。"""


def munger_agent(ctx: dict) -> dict:
    fund = ctx.get("fund", {})
    tags = ctx.get("tags", [])
    reasons = []
    score = 0

    roe = fund.get("roe")
    if roe is not None:
        if roe >= 25:
            score += 3; reasons.append(f"ROE {roe:.0f}% 複利引擎")
        elif roe >= 18:
            score += 2; reasons.append(f"ROE {roe:.0f}% 優秀")
        elif roe >= 12:
            score += 1
        elif roe < 8:
            score -= 2; reasons.append(f"ROE {roe:.0f}% 不足")

    debt = fund.get("debt_ratio")
    if debt is not None:
        if debt < 30:
            score += 1; reasons.append("財務穩健")
        elif debt > 60:
            score -= 1

    # 護城河代理：在 AI 供應鏈中（Munger 買 BYD 的邏輯 — 產業地位）
    has_moat_tag = any(t.get("tag") == "AI" for t in tags) if tags else False
    if has_moat_tag:
        score += 1; reasons.append("AI 產業地位")

    pe = fund.get("pe_ratio")
    if pe is not None and pe > 0:
        if pe > 35:
            score -= 2; reasons.append(f"PE {pe:.0f} 過高（不付冤枉錢）")
        elif pe > 25:
            score -= 1

    ni_yoy = fund.get("ni_yoy")
    rev_yoy = fund.get("revenue_yoy")
    if ni_yoy is not None and ni_yoy > 8 and rev_yoy is not None and rev_yoy > 5:
        score += 1; reasons.append("營收獲利雙成長")

    if score >= 4:
        return {"signal": "bullish", "confidence": min(1.0, score / 7), "reasons": reasons}
    if score <= -2:
        return {"signal": "bearish", "confidence": min(1.0, -score / 4), "reasons": reasons}
    return {"signal": "neutral", "confidence": 0.3, "reasons": reasons}
