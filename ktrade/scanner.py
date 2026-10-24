"""Find the liquid coins whose volatility is expanding in a clear direction."""
import logging
from typing import Any, Dict, List

import pandas as pd

from ktrade.config import Settings
from ktrade.delta import Delta, DeltaError
from ktrade.signals import features

log = logging.getLogger("ktrade.scanner")
MIN_HISTORY_BARS = 24 * 30


def universe(products: Dict[str, Dict], tickers: Dict[str, Dict], s: Settings, now: float) -> List[str]:
    out = []
    for sym, p in products.items():
        t = tickers.get(sym)
        if not t or sym in s.exclude or t["turnover_usd"] < s.min_turnover_usd:
            continue
        if not p["launched"] or (now - p["launched"]) / 86400 < s.min_listing_days:
            continue                                            # fresh listings are where pump-and-dumps live
        spread_bps = (t["ask"] - t["bid"]) / ((t["ask"] + t["bid"]) / 2) * 1e4
        if spread_bps > s.max_spread_bps:
            continue
        out.append(sym)
    return out


def correlation(returns: Dict[str, pd.Series], a: str, b: str) -> float:
    """Correlation of the last 7 days of hourly returns; 0 when either history is missing."""
    if a not in returns or b not in returns:
        return 0.0
    c = returns[a].corr(returns[b])
    return 0.0 if pd.isna(c) else float(c)


def scan(delta: Delta, s: Settings, now: float, products=None, tickers=None) -> Dict[str, Any]:
    products = products or delta.perpetuals()
    tickers = tickers or delta.tickers()
    syms = universe(products, tickers, s, now)
    rows, wild, returns = [], [], {}
    for sym in syms:
        try:
            df = delta.candles(sym, "1h", days=40)
        except DeltaError as e:
            log.warning("candles %s: %s", sym, e)
            continue
        if len(df) < MIN_HISTORY_BARS:
            continue
        f = features(df)
        returns[sym] = df["close"].pct_change().tail(168).reset_index(drop=True)
        t = tickers[sym]
        f.update(symbol=sym, contract_value=products[sym]["contract_value"], turnover_musd=round(t["turnover_usd"] / 1e6, 1),
                 funding_rate_pct_8h=t["funding_rate"],
                 spread_bps=round((t["ask"] - t["bid"]) / ((t["ask"] + t["bid"]) / 2) * 1e4, 2))
        if f["atr_pct"] > s.max_atr_pct or abs(f["ret_24h_pct"]) > s.max_move_24h_pct:
            wild.append({"symbol": sym, "atr_pct": round(f["atr_pct"], 2), "ret_24h_pct": round(f["ret_24h_pct"], 1)})
            continue                                             # a frenzy, not a trend: stops would be miles away
        f["score"] = min(f["vol_ratio"], s.vol_ratio_cap) * abs(f["trend"])     # expanding volatility AND a direction
        rows.append(f)
    rows.sort(key=lambda r: r["score"], reverse=True)
    chosen, duplicates = [], []
    for r in rows:
        # Same direction + moving in step = the same bet. Opposite directions on twins would be a hedge, not a double.
        twin = next((c["symbol"] for c in chosen if correlation(returns, r["symbol"], c["symbol"])
                     * (1 if r["trend"] * c["trend"] >= 0 else -1) > s.max_correlation), None)
        if twin:
            duplicates.append({"symbol": r["symbol"], "same_bet_as": twin})
        else:
            chosen.append(r)
    return {"universe": syms, "candidates": chosen[: s.max_candidates], "scanned": len(rows), "too_wild": wild,
            "duplicates": duplicates, "returns": returns}
