"""
Portfolio backtest of the rules-only book, bar by bar, with the live risk engine.

Signals use only closed bars. Entries fill at the signal bar's close plus slippage (a stand-in for the next
open). Stops and targets are checked against each later bar's open, high and low; when a gap jumps past a
stop the fill is the open, and when one bar touches both stop and target the stop is assumed (conservative).
Costs: Delta taker fee + 18% GST and slippage on both sides, perpetual funding every bar (longs pay, shorts
receive a typical +0.01%/8h). Quantities are fractional (no contract rounding).
"""
from dataclasses import dataclass, replace
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from ktrade.history import resample
from ktrade.signals import atr as atr_series

FEE = 0.0005 * 1.18
SLIP = 0.0003
FUNDING_8H = 0.0001
IST = 19800


@dataclass(frozen=True)
class Rules:
    name: str
    bar_hours: int = 1
    stop_atr: float = 3.0
    target_r: Optional[float] = 2.0        # None = no fixed target
    max_hold_bars: Optional[int] = 48      # None = no time limit
    trail: bool = False                    # stop follows the close by stop_atr x ATR and never loosens
    trend_exit: bool = False               # close when the trend score stops supporting the position
    long_only: bool = False
    trend_entry: float = 0.5
    risk: float = 0.005
    max_positions: int = 3
    max_gross: float = 2.0
    daily_loss: float = 0.03
    max_atr_pct: float = 3.0               # for 1-hour bars; scaled by sqrt(bar_hours)
    max_move_24h: float = 25.0
    vol_cap: float = 3.0
    max_corr: float = 0.85
    candidates: int = 5


# --- per-coin features, vectorised over all bars --------------------------------------------------
def _donchian_states(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int, m: int) -> np.ndarray:
    hi = pd.Series(h).rolling(n).max().shift(1).to_numpy()
    lo = pd.Series(l).rolling(n).min().shift(1).to_numpy()
    xhi = pd.Series(h).rolling(m).max().shift(1).to_numpy()
    xlo = pd.Series(l).rolling(m).min().shift(1).to_numpy()
    out, state = np.zeros(len(c)), 0
    for i in range(len(c)):
        if state == 1 and c[i] < xlo[i]:
            state = 0
        elif state == -1 and c[i] > xhi[i]:
            state = 0
        if c[i] > hi[i]:
            state = 1
        elif c[i] < lo[i]:
            state = -1
        out[i] = state
    return out


def features(df: pd.DataFrame, bar_hours: int) -> pd.DataFrame:
    """Same definitions as ktrade.signals, computed for every bar at once (identical windows in bars)."""
    c, h, l = df["close"], df["high"], df["low"]
    a = atr_series(df)
    e50, e200 = c.ewm(span=50, adjust=False).mean(), c.ewm(span=200, adjust=False).mean()
    ema = np.where((e50 > e200) & (c > e200), 1.0, np.where((e50 < e200) & (c < e200), -1.0, 0.0))
    tsmom = np.sign(c / c.shift(168) - 1).fillna(0).to_numpy()
    trend = (ema + _donchian_states(h.to_numpy(), l.to_numpy(), c.to_numpy(), 55, 20)
             + _donchian_states(h.to_numpy(), l.to_numpy(), c.to_numpy(), 168, 72) + tsmom) / 4
    per_day = 24 // bar_hours
    return pd.DataFrame({
        "time": df["time"], "open": df["open"], "high": h, "low": l, "close": c, "atr": a,
        "atr_pct": a / c * 100, "vol_ratio": a / a.rolling(30 * per_day, min_periods=10 * per_day).median(),
        "ret24": (c / c.shift(per_day) - 1) * 100, "trend": trend, "ret": c.pct_change(),
    })


def panel(hourly: Dict[str, pd.DataFrame], bar_hours: int) -> Dict[str, np.ndarray]:
    """Align every coin on one time grid: arrays shaped [bars, coins], NaN where a coin has no bar."""
    feats = {s: features(resample(df, bar_hours), bar_hours).set_index("time") for s, df in hourly.items()}
    times = sorted(set().union(*[f.index for f in feats.values()]))
    out = {"time": np.array(times, dtype=np.int64), "symbols": list(feats)}
    for col in ("open", "high", "low", "close", "atr", "atr_pct", "vol_ratio", "ret24", "trend", "ret"):
        out[col] = np.column_stack([feats[s][col].reindex(times).to_numpy(dtype=float) for s in feats])
    return out


# --- the simulation -------------------------------------------------------------------------------
def run(p: Dict[str, np.ndarray], r: Rules, start_equity: float = 1000.0, start_time: Optional[int] = None) -> Dict:
    T, N = p["close"].shape
    o, hi, lo, c, at = p["open"], p["high"], p["low"], p["close"], p["atr"]
    funding_per_bar = FUNDING_8H * r.bar_hours / 8
    max_atr = r.max_atr_pct * np.sqrt(r.bar_hours)
    warm = 30 * 24 // r.bar_hours + 200
    cash, pos, trades = start_equity, [], []
    equity = np.full(T, np.nan)
    day, day_start = None, start_equity

    def close(ps, i, px, reason):
        nonlocal cash
        units = ps["qty"]
        gross = (px - ps["entry"]) * ps["side"] * units
        fee = units * px * FEE
        cash += gross - fee
        trades.append({"symbol": p["symbols"][ps["j"]], "side": ps["side"], "entry_time": int(p["time"][ps["i"]]),
                       "exit_time": int(p["time"][i]), "entry": ps["entry"], "exit": px, "reason": reason,
                       "net": gross - fee - ps["entry_fee"] - ps["funding"], "risk": ps["risk"],
                       "bars": i - ps["i"]})

    first = max(warm, int(np.searchsorted(p["time"], start_time)) if start_time else warm)
    for i in range(first, T):
        # 1) Exits during bar i (positions were opened at an earlier close).
        still = []
        for ps in pos:
            j, s = ps["j"], ps["side"]
            if np.isnan(c[i, j]):
                still.append(ps)
                continue
            fund = funding_per_bar * ps["qty"] * c[i, j] * s
            cash -= fund
            ps["funding"] += fund
            stop, tgt = ps["stop"], ps["target"]
            if (o[i, j] - stop) * s <= 0:
                close(ps, i, o[i, j] * (1 - s * SLIP), "stop (gap)")
            elif (lo[i, j] if s > 0 else hi[i, j]) * s <= stop * s:
                close(ps, i, stop * (1 - s * SLIP), "stop")
            elif tgt is not None and (hi[i, j] if s > 0 else lo[i, j]) * s >= tgt * s:
                close(ps, i, max(tgt * s, o[i, j] * s) * s, "target")
            elif r.trend_exit and p["trend"][i, j] * s <= 0:
                close(ps, i, c[i, j] * (1 - s * SLIP), "trend over")
            elif r.max_hold_bars and i - ps["i"] >= r.max_hold_bars:
                close(ps, i, c[i, j] * (1 - s * SLIP), "time limit")
            else:
                if r.trail and not np.isnan(at[i, j]):
                    ps["stop"] = max(stop, c[i, j] - r.stop_atr * at[i, j]) if s > 0 else min(stop, c[i, j] + r.stop_atr * at[i, j])
                still.append(ps)
        pos = still

        # 2) Mark to market at the close.
        unreal = sum((c[i, ps["j"]] - ps["entry"]) * ps["side"] * ps["qty"] for ps in pos if not np.isnan(c[i, ps["j"]]))
        eq = cash + unreal
        equity[i] = eq
        d = (int(p["time"][i]) + IST) // 86400
        if d != day:
            day, day_start = d, eq

        # 3) Entries at the close, same scanner and risk engine as live.
        if len(pos) >= r.max_positions or (day_start - eq) / day_start >= r.daily_loss or i < warm:
            continue
        tr, vr, ap, r24 = p["trend"][i], p["vol_ratio"][i], p["atr_pct"][i], p["ret24"][i]
        ok = ~np.isnan(c[i]) & ~np.isnan(vr) & ~np.isnan(at[i]) & (ap <= max_atr) & (np.abs(r24) <= r.max_move_24h)
        score = np.where(ok, np.minimum(vr, r.vol_cap) * np.abs(tr), -1.0)
        order = [j for j in np.argsort(-score) if score[j] > 0]
        rets = p["ret"][max(0, i - 167): i + 1]
        chosen: List[int] = []
        for j in order:
            if len(chosen) == r.candidates:
                break
            same = any(_corr(rets, j, k) * (1 if tr[j] * tr[k] >= 0 else -1) > r.max_corr for k in chosen)
            if not same:
                chosen.append(j)
        held = {ps["j"]: ps["side"] for ps in pos}
        for j in chosen:
            if len(pos) >= r.max_positions:
                break
            side = 1 if tr[j] >= r.trend_entry else -1 if tr[j] <= -r.trend_entry and not r.long_only else 0
            if side == 0 or j in held:
                continue
            if any(_corr(rets, j, k) * side * s > r.max_corr for k, s in held.items()):
                continue
            px = c[i, j] * (1 + side * SLIP)
            dist = r.stop_atr * at[i, j]
            per_unit = dist + (2 * FEE + 2 * SLIP) * px
            gross = sum(ps["qty"] * c[i, ps["j"]] for ps in pos if not np.isnan(c[i, ps["j"]]))
            qty = min(eq * r.risk / per_unit, max(0.0, r.max_gross * eq - gross) / px)
            if qty * px < 1:
                continue
            fee = qty * px * FEE
            cash -= fee
            ps = {"j": j, "side": side, "qty": qty, "entry": px, "i": i, "stop": px - side * dist,
                  "target": px + side * r.target_r * dist if r.target_r else None, "entry_fee": fee,
                  "funding": 0.0, "risk": qty * per_unit}
            pos.append(ps)
            held[j] = side

    return {"rules": r, "time": p["time"], "equity": equity, "trades": trades}


def _corr(rets: np.ndarray, a: int, b: int) -> float:
    x, y = rets[:, a], rets[:, b]
    m = ~np.isnan(x) & ~np.isnan(y)
    if m.sum() < 30:
        return 0.0
    cc = np.corrcoef(x[m], y[m])[0, 1]
    return 0.0 if np.isnan(cc) else float(cc)


# --- reporting ------------------------------------------------------------------------------------
def metrics(res: Dict, t0: int, t1: int) -> Dict:
    """Performance between two timestamps, from the equity curve and the trades opened in that window."""
    t, eq = res["time"], res["equity"]
    m = (t >= t0) & (t < t1) & ~np.isnan(eq)
    if m.sum() < 10:
        return {}
    e = eq[m]
    years = (t[m][-1] - t[m][0]) / (365.25 * 86400)
    rets = np.diff(e) / e[:-1]
    bars_per_year = 365.25 * 24 / res["rules"].bar_hours
    tr = [x for x in res["trades"] if t0 <= x["entry_time"] < t1]
    wins, losses = [x["net"] for x in tr if x["net"] > 0], [x["net"] for x in tr if x["net"] <= 0]
    years_ret = {}
    for y in sorted(set(pd.to_datetime(t[m], unit="s").year)):
        ym = m & (pd.to_datetime(t, unit="s").year == y)
        ey = eq[ym]
        years_ret[int(y)] = round((ey[-1] / ey[0] - 1) * 100, 1) if len(ey) > 1 else 0.0
    return {
        "return_pct": round((e[-1] / e[0] - 1) * 100, 1),
        "cagr_pct": round(((e[-1] / e[0]) ** (1 / years) - 1) * 100, 1) if years > 0.2 and e[-1] > 0 else None,
        "max_drawdown_pct": round(float(((e - np.maximum.accumulate(e)) / np.maximum.accumulate(e)).min()) * 100, 1),
        "sharpe": round(float(rets.mean() / rets.std() * np.sqrt(bars_per_year)), 2) if rets.std() > 0 else 0.0,
        "trades": len(tr), "trades_per_month": round(len(tr) / (years * 12), 1) if years else None,
        "win_rate_pct": round(len(wins) / len(tr) * 100, 1) if tr else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "avg_trade_r": round(float(np.mean([x["net"] / x["risk"] for x in tr if x["risk"] > 0])), 3) if tr else None,
        "by_year": years_ret,
    }


def buy_and_hold(p: Dict, symbols: List[str], t0: int, t1: int) -> Dict:
    """Equal-weight buy and hold of `symbols`, no costs, for context."""
    idx = [p["symbols"].index(s) for s in symbols if s in p["symbols"]]
    m = (p["time"] >= t0) & (p["time"] < t1)
    c = p["close"][m][:, idx]
    c = c[~np.isnan(c).any(axis=1)]
    rel = (c / c[0]).mean(axis=1)
    years = m.sum() / (365.25 * 24 / max(1, int(round((p["time"][1] - p["time"][0]) / 3600))))
    return {"return_pct": round((rel[-1] - 1) * 100, 1),
            "max_drawdown_pct": round(float(((rel - np.maximum.accumulate(rel)) / np.maximum.accumulate(rel)).min()) * 100, 1),
            "cagr_pct": round((rel[-1] ** (1 / years) - 1) * 100, 1) if years > 0.2 else None}
