"""
投資大師 Agent 評分模組（規則式，非 LLM）。

每位大師接收同一份 ctx（fund/tech/monthly/tags 等），回傳：
{
    "signal": "bullish" | "neutral" | "bearish",
    "confidence": 0.0 ~ 1.0,
    "reasons": ["理由1", "理由2", ...]
}

apply_agents(ctx) 會跑所有大師，彙總為：
{
    "agent_score": 0.0 ~ 1.0,      # 平均權重分數（bullish=1, neutral=0.5, bearish=0）
    "consensus": {"bullish": 5, "neutral": 1, "bearish": 1},
    "bonus": -0.05 ~ +0.05,         # 套用到規則引擎的軟加分
    "details": [{name, signal, confidence, reasons}, ...]
}
"""
from .buffett import buffett_agent
from .graham import graham_agent
from .munger import munger_agent
from .fisher import fisher_agent
from .druckenmiller import druckenmiller_agent
from .wood import wood_agent
from .ackman import ackman_agent


AGENTS = [
    ("Buffett", buffett_agent),
    ("Graham", graham_agent),
    ("Munger", munger_agent),
    ("Fisher", fisher_agent),
    ("Druckenmiller", druckenmiller_agent),
    ("Wood", wood_agent),
    ("Ackman", ackman_agent),
]


_SIGNAL_WEIGHT = {"bullish": 1.0, "neutral": 0.5, "bearish": 0.0}


def apply_agents(ctx: dict) -> dict:
    """跑全部大師 agent，彙總結果。"""
    details = []
    consensus = {"bullish": 0, "neutral": 0, "bearish": 0}
    weighted_sum = 0.0
    total_weight = 0.0

    for name, fn in AGENTS:
        try:
            res = fn(ctx)
        except Exception as e:
            res = {"signal": "neutral", "confidence": 0.0, "reasons": [f"錯誤: {e}"]}
        sig = res.get("signal", "neutral")
        conf = float(res.get("confidence", 0.0))
        consensus[sig] = consensus.get(sig, 0) + 1
        # 平均權重：每位大師權重 1.0，confidence 調整
        weighted_sum += _SIGNAL_WEIGHT[sig] * (0.5 + 0.5 * conf)
        total_weight += 1.0
        details.append({"name": name, "signal": sig, "confidence": conf, "reasons": res.get("reasons", [])})

    agent_score = weighted_sum / total_weight if total_weight > 0 else 0.5

    # 軟加分：agent_score 0.5 為中性，±0.05 範圍
    # score 0.7 以上 → +0.05，0.3 以下 → -0.05，線性
    bonus = (agent_score - 0.5) * 0.25  # 最多 ±0.125，但我們 clip 到 ±0.05
    bonus = max(-0.05, min(0.05, bonus))

    # 動能派否決：Druckenmiller 看空時（股價 / 趨勢弱），不讓基本面派給正向 bonus
    # 理由：基本面好但股價持續下跌，市場通常已 price-in 壞消息
    druckenmiller_res = next((d for d in details if d["name"] == "Druckenmiller"), None)
    if druckenmiller_res and druckenmiller_res["signal"] == "bearish" and bonus > 0:
        bonus = 0.0

    return {
        "agent_score": round(agent_score, 3),
        "consensus": consensus,
        "bonus": round(bonus, 4),
        "details": details,
    }
