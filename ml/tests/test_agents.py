"""Agent 大師評分模組測試"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents import apply_agents, AGENTS
from agents.buffett import buffett_agent
from agents.graham import graham_agent
from agents.munger import munger_agent
from agents.fisher import fisher_agent
from agents.druckenmiller import druckenmiller_agent
from agents.wood import wood_agent
from agents.ackman import ackman_agent


def _base_ctx():
    return {"fund": {}, "tech": {}, "monthly": {}, "tags": []}


def test_all_agents_return_valid_schema():
    """所有 agent 都回傳正確的 schema"""
    ctx = _base_ctx()
    for name, fn in AGENTS:
        res = fn(ctx)
        assert "signal" in res
        assert res["signal"] in ("bullish", "neutral", "bearish"), f"{name}: {res['signal']}"
        assert 0.0 <= res.get("confidence", 0) <= 1.0, f"{name}: {res['confidence']}"
        assert isinstance(res.get("reasons", []), list)


def test_buffett_bullish_on_quality():
    """Buffett 對高 ROE + 低負債 + 合理 PE 看多"""
    ctx = {"fund": {"roe": 25, "debt_ratio": 30, "ni_yoy": 15, "pe_ratio": 15}}
    res = buffett_agent(ctx)
    assert res["signal"] == "bullish"


def test_buffett_bearish_on_loss():
    """Buffett 對虧損看空"""
    ctx = {"fund": {"ni_ttm": -1e8, "roe": -5}}
    res = buffett_agent(ctx)
    assert res["signal"] == "bearish"


def test_graham_bullish_on_deep_value():
    """Graham 對低 PE + 低 PB + 高殖利率看多"""
    ctx = {"fund": {"pe_ratio": 10, "pb_ratio": 1.2, "div_yield": 5}}
    res = graham_agent(ctx)
    assert res["signal"] == "bullish"


def test_graham_avoids_loss():
    """Graham 避開虧損"""
    ctx = {"fund": {"ni_ttm": -1e8, "pe_ratio": 10}}
    res = graham_agent(ctx)
    assert res["signal"] == "bearish"


def test_wood_prefers_innovation():
    """Wood 對創新主題 + 高成長看多"""
    ctx = {
        "fund": {"revenue_yoy": 35},
        "tags": [
            {"tag": "AI", "sub_tag": "GPU/AI晶片設計"},
            {"tag": "AI", "sub_tag": "機器人"},
        ],
    }
    res = wood_agent(ctx)
    assert res["signal"] == "bullish"


def test_wood_bearish_on_traditional_slow_growth():
    """Wood 對非創新 + 低成長看空"""
    ctx = {"fund": {"revenue_yoy": 3}, "tags": []}
    res = wood_agent(ctx)
    assert res["signal"] == "bearish"


def test_druckenmiller_follows_trend():
    """Druckenmiller 對高 RS + 趨勢看多"""
    ctx = {
        "tech": {"return20d": 12},
        "minervini": {"minervini": True},
        "rs_pctile": 90,
        "fund": {"ni_ttm": 1e8},
    }
    res = druckenmiller_agent(ctx)
    assert res["signal"] == "bullish"


def test_fisher_likes_high_growth():
    """Fisher 對高成長看多"""
    ctx = {
        "fund": {"revenue_yoy": 30, "ni_yoy": 40, "roe": 18},
        "monthly": {"rev_consecutive_yoy": 8, "rev_accel": True},
    }
    res = fisher_agent(ctx)
    assert res["signal"] == "bullish"


def test_munger_needs_moat():
    """Munger 對高 ROE + AI 產業看多"""
    ctx = {
        "fund": {"roe": 28, "debt_ratio": 25, "ni_yoy": 12, "revenue_yoy": 10},
        "tags": [{"tag": "AI", "sub_tag": "晶圓代工"}],
    }
    res = munger_agent(ctx)
    assert res["signal"] == "bullish"


def test_ackman_quality_at_reasonable_price():
    """Ackman QARP"""
    ctx = {"fund": {"roe": 22, "ni_yoy": 20, "revenue_yoy": 15, "pe_ratio": 18, "debt_ratio": 40, "ni_ttm": 1e8}}
    res = ackman_agent(ctx)
    assert res["signal"] == "bullish"


def test_apply_agents_aggregation():
    """apply_agents 回傳正確的彙總結構"""
    ctx = {
        "fund": {"roe": 25, "debt_ratio": 30, "ni_yoy": 20, "revenue_yoy": 15, "pe_ratio": 18, "ni_ttm": 1e8, "pb_ratio": 2},
        "tags": [{"tag": "AI", "sub_tag": "GPU/AI晶片設計"}],
    }
    res = apply_agents(ctx)
    assert "agent_score" in res
    assert "consensus" in res
    assert "bonus" in res
    assert "details" in res
    assert len(res["details"]) == 7
    assert -0.05 <= res["bonus"] <= 0.05
    # 優質股應該被多數大師看多
    assert res["consensus"]["bullish"] >= 3


def test_apply_agents_bonus_range():
    """bonus 永遠在 ±0.05 範圍內"""
    ctx = _base_ctx()
    res = apply_agents(ctx)
    assert -0.05 <= res["bonus"] <= 0.05
