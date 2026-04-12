"""Cathie Wood：破壞性創新（AI / 生技 / 機器人 / 能源轉型），忽略短期估值。"""

# Wood 偏好的創新主題
INNOVATION_SUB_TAGS = {
    "GPU/AI晶片設計", "ASIC客製晶片", "AI PC/邊緣運算", "AI手機",
    "機器人", "CPO矽光子", "SiC碳化矽", "軟體/SaaS", "雲端服務",
    "醫療AI", "車用AI", "智慧工廠", "光通訊模組",
}


def wood_agent(ctx: dict) -> dict:
    fund = ctx.get("fund", {})
    tags = ctx.get("tags", [])
    reasons = []
    score = 0

    # 創新主題命中
    sub_tags = {t.get("sub_tag") for t in tags if t.get("tag") == "AI" and t.get("sub_tag")}
    hits = sub_tags & INNOVATION_SUB_TAGS
    if hits:
        if len(hits) >= 2:
            score += 3; reasons.append(f"多個創新主題：{', '.join(list(hits)[:3])}")
        else:
            score += 2; reasons.append(f"創新主題：{list(hits)[0]}")

    # 高成長（Wood 只看 TAM + 成長，不看估值）
    rev_yoy = fund.get("revenue_yoy")
    if rev_yoy is not None:
        if rev_yoy >= 30:
            score += 2; reasons.append(f"營收高成長 {rev_yoy:.0f}%")
        elif rev_yoy >= 15:
            score += 1

    # 研發密度的代理：獲利率變動（研發投入多 → 短期利潤率壓縮）
    # 如果營收高成長但獲利成長趨緩，Wood 不在意
    ni_yoy = fund.get("ni_yoy")
    if rev_yoy is not None and rev_yoy >= 20 and ni_yoy is not None:
        # Wood 允許獲利短期犧牲換成長
        pass

    # 完全沒有創新主題 → bearish（不在 Wood 的守備範圍）
    if not hits and rev_yoy is not None and rev_yoy < 10:
        score -= 2; reasons.append("非創新主題")

    if score >= 3:
        return {"signal": "bullish", "confidence": min(1.0, score / 5), "reasons": reasons}
    if score <= -2:
        return {"signal": "bearish", "confidence": min(1.0, -score / 3), "reasons": reasons}
    return {"signal": "neutral", "confidence": 0.3, "reasons": reasons}
