"""Per-coin features from 1-hour candles. Everything uses closed candles only."""
import math
from typing import Any, Dict

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()                    # Wilder smoothing


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def _donchian(df: pd.DataFrame, n: int, m: int) -> float:
    """+1 long / -1 short / 0 flat state of an n-bar breakout system with an m-bar exit channel."""
    hi, lo = df["high"].rolling(n).max().shift(1), df["low"].rolling(n).min().shift(1)
    xhi, xlo = df["high"].rolling(m).max().shift(1), df["low"].rolling(m).min().shift(1)
    state = 0
    for c, a, b, xa, xb in zip(df["close"], hi, lo, xhi, xlo):
        if state == 1 and c < xb:
            state = 0
        elif state == -1 and c > xa:
            state = 0
        if c > a:
            state = 1
        elif c < b:
            state = -1
    return float(state)


def trend_score(df: pd.DataFrame) -> float:
    """Average of four classic trend models, from -1 (all short) to +1 (all long)."""
    c = df["close"]
    e50, e200 = c.ewm(span=50, adjust=False).mean().iloc[-1], c.ewm(span=200, adjust=False).mean().iloc[-1]
    ema = 1.0 if e50 > e200 and c.iloc[-1] > e200 else -1.0 if e50 < e200 and c.iloc[-1] < e200 else 0.0
    tsmom = float(np.sign(c.iloc[-1] / c.iloc[-169] - 1)) if len(c) > 169 else 0.0      # 7-day momentum
    return (ema + _donchian(df, 55, 20) + _donchian(df, 168, 72) + tsmom) / 4


def features(df: pd.DataFrame) -> Dict[str, Any]:
    a = atr(df)
    c = df["close"]
    last = float(c.iloc[-1])
    baseline_atr = float(a.tail(24 * 30).median())
    out = {
        "price": last,
        "atr": float(a.iloc[-1]),
        "atr_pct": float(a.iloc[-1] / last * 100),
        "vol_ratio": float(a.iloc[-1] / baseline_atr) if baseline_atr else 1.0,   # >1 = volatility expanding
        "ret_24h_pct": float((last / c.iloc[-25] - 1) * 100) if len(c) > 25 else 0.0,
        "ret_7d_pct": float((last / c.iloc[-169] - 1) * 100) if len(c) > 169 else 0.0,
        "trend": trend_score(df),
        "rsi14": float(rsi(c).iloc[-1]),
        "bars": int(len(df)),
    }
    # A coin that has not moved gives 0/0 in RSI; never let NaN reach the risk engine, Claude or the JSON API.
    return {k: (50.0 if k == "rsi14" else 0.0) if isinstance(v, float) and not math.isfinite(v) else v for k, v in out.items()}
