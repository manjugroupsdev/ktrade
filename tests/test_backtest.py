import numpy as np
import pandas as pd

from ktrade.backtest import Rules, metrics, panel, run


def hourly(drift, n=2000, seed=0, crash_at=None):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(np.full(n, drift / n) + rng.normal(0, 0.002, n)))
    o = np.r_[c[0], c[:-1]]
    if crash_at:
        c[crash_at:] *= 0.8
        o[crash_at] = c[crash_at]                                   # opens 20% lower: a gap through any stop
    h, l = np.maximum(o, c) * 1.001, np.minimum(o, c) * 0.999
    return pd.DataFrame({"time": 1_600_000_000 + np.arange(n) * 3600, "open": o, "high": h, "low": l, "close": c, "volume": 1.0})


def test_uptrend_makes_money_on_longs_only():
    p = panel({"UP": hourly(0.8, seed=1)}, 1)
    res = run(p, Rules("t", trail=True, target_r=None, max_hold_bars=None, trend_exit=True))
    assert res["trades"] and all(t["side"] == 1 for t in res["trades"])
    m = metrics(res, 0, 2**40)
    assert m["return_pct"] > 0 and m["max_drawdown_pct"] > -10


def test_gap_through_the_stop_fills_at_the_open_not_the_stop():
    p = panel({"UP": hourly(0.8, seed=1, crash_at=1500)}, 1)
    res = run(p, Rules("t", trail=True, target_r=None, max_hold_bars=None, trend_exit=True))
    gap = [t for t in res["trades"] if t["reason"] == "stop (gap)"]
    assert gap and gap[0]["net"] < -gap[0]["risk"]               # a gap can lose more than the planned risk
