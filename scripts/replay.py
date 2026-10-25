"""
Replay the LIVE engine (ktrade.engine.Engine: the same scanner, risk engine, trailing stops, trend exits and
ledger that run on the Mac) over history, hour by hour, as if it had been running since a start date.

    .venv/bin/python scripts/replay.py --start 2024-01-01

Unlike scripts/research.py (a separate fast simulator), this drives the production code path. Prices come from
the cached hourly history; Delta's real contract sizes are used, so whole-contract rounding is included. Each
hour is fed as four ticks (open, low, high, close). With --fills path (default) a stop crossed inside the hour
fills where price crossed it, as the live 30-second check does; gaps at the open fill at the open.
With --fills extreme every stop fills at the hour's worst price instead (very pessimistic). Entries fill at the open of the hour
after a 4-hour candle closes. The AI book abstains: replaying Claude on past dates would leak hindsight.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import replace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ktrade.backtest import Rules, metrics, panel, run          # noqa: E402
from ktrade.config import Settings                              # noqa: E402
from ktrade.delta import Delta                                  # noqa: E402
from ktrade.engine import Engine                                # noqa: E402
from ktrade.history import UNIVERSE, hourly, resample           # noqa: E402
from ktrade.ledger import Ledger                                # noqa: E402


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class HistoricalDelta:
    """Answers the engine's three market-data calls as of the replay clock."""

    def __init__(self, hourly_frames, bar_hours, contract_values, clock):
        self.clock, self.prices, self.bar_sec = clock, {}, bar_hours * 3600
        self.bars = {s: resample(df, bar_hours) for s, df in hourly_frames.items()}
        self.times = {s: df["time"].to_numpy() for s, df in self.bars.items()}
        self.products = {s: {"contract_value": cv, "tick": 0.0, "taker_fee": 0.0005, "launched": 1.0}
                         for s, cv in contract_values.items()}

    def perpetuals(self):
        return self.products

    def tickers(self):
        return {s: {"bid": p * (1 - 1e-4), "ask": p * (1 + 1e-4), "last": p, "turnover_usd": 1e9,
                    "funding_rate": 0.01, "ts": self.clock()} for s, p in self.prices.items()}

    def candles(self, symbol, resolution="4h", days=100):
        t, now = self.times[symbol], self.clock()
        hi = np.searchsorted(t, now - self.bar_sec, side="right")          # closed bars only
        lo = np.searchsorted(t, now - days * 86400)
        return self.bars[symbol].iloc[lo:hi].reset_index(drop=True)


class Abstain:
    def decide(self, candidates, positions, equity):
        return {"market_view": "replay: the AI book does not trade", "entries": [], "exits": [], "cost_usd": 0.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--risk", type=float, default=None, help="risk per trade (default: your .env)")
    ap.add_argument("--fills", choices=["path", "extreme"], default="path",
                    help="path: stops fill where price crosses them (as the 30-second live check does); "
                         "extreme: at the hour's worst price (very pessimistic)")
    a = ap.parse_args()
    live = Settings.from_env()
    s = replace(live, db_path=os.path.join(tempfile.mkdtemp(), "replay.db"),
                risk_per_trade=a.risk if a.risk is not None else live.risk_per_trade, claude_web=False)
    start = int(pd.Timestamp(a.start, tz="UTC").timestamp())

    coins = {f"{c}USD": hourly(c, refresh_after_s=1e12) for c in UNIVERSE}
    delta_products = Delta(s.delta_base_url).perpetuals()
    cvs = {sym: delta_products[sym]["contract_value"] for sym in coins if sym in delta_products}
    coins = {sym: df for sym, df in coins.items() if sym in cvs}
    print(f"replaying {len(coins)} Delta-listed coins from {a.start} | risk {s.risk_per_trade:.1%} per trade | "
          f"{s.bar_hours}h bars | trailing {s.trail} | trend exit {s.trend_exit}", flush=True)

    clock = Clock()
    hist = HistoricalDelta(coins, s.bar_hours, cvs, clock)
    ledger = Ledger(s.db_path, s.start_equity, clock)
    engine = Engine(s, hist, ledger, Abstain(), clock)
    ledger.summary = lambda book, marks: {"equity": ledger.equity(book, marks)}     # reporting only, kept cheap

    hours = sorted(set().union(*[set(df["time"][df["time"] >= start]) for df in coins.values()]))
    rows = {sym: df.set_index("time") for sym, df in coins.items()}
    t0 = time.time()
    for n, h in enumerate(hours):
        bar = {sym: r.loc[h] for sym, r in rows.items() if h in r.index}
        for offset, field in ((121, "open"), (900, "low"), (1800, "high"), (3599, "close")):
            target = {sym: float(b[field]) for sym, b in bar.items()}
            if field in ("low", "high") and a.fills == "path":
                # Prices move continuously: on the way to the hour's extreme they pass through any stop in between,
                # and the live bot (checking every 30 s) sees that crossing first. Gaps at the open are not softened.
                crossed = {}
                for p in ledger.positions():
                    x, stop, side = target.get(p["symbol"]), p["stop"], p["side"]
                    if x is not None and (x - stop) * side <= 0 and (hist.prices.get(p["symbol"], x) - stop) * side > 0:
                        crossed[p["symbol"]] = stop
                if crossed:
                    clock.t = h + offset - 1
                    hist.prices = {**hist.prices, **crossed}
                    engine.tick()
            clock.t = h + offset
            hist.prices = target
            engine.tick()
        if n % 2000 == 0:
            print(f"  {pd.Timestamp(h, unit='s').date()}  equity ${ledger.equity('baseline', hist.tickers()):,.2f}  "
                  f"({time.time() - t0:.0f}s)", flush=True)

    marks = hist.tickers()
    trades = ledger.trades("baseline", 100000)
    curve = np.array([e for _, e in ledger.curve("baseline")] + [ledger.equity("baseline", marks)])
    times = np.array([t for t, _ in ledger.curve("baseline")] + [clock.t])
    years = (times[-1] - times[0]) / (365.25 * 86400)
    dd = float(((curve - np.maximum.accumulate(curve)) / np.maximum.accumulate(curve)).min())
    wins, losses = [t["net"] for t in trades if t["net"] > 0], [t["net"] for t in trades if t["net"] <= 0]
    by_year = {}
    for y in sorted(set(pd.to_datetime(times, unit="s").year)):
        m = pd.to_datetime(times, unit="s").year == y
        by_year[int(y)] = round((curve[m][-1] / curve[m][0] - 1) * 100, 1)
    live_code = {"start_equity": s.start_equity, "end_equity": round(float(curve[-1]), 2),
                 "return_pct": round((curve[-1] / curve[0] - 1) * 100, 1),
                 "cagr_pct": round(((curve[-1] / curve[0]) ** (1 / years) - 1) * 100, 1), "max_drawdown_pct": round(dd * 100, 1),
                 "trades": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
                 "profit_factor": round(sum(wins) / -sum(losses), 2) if losses else None,
                 "avg_r": round(float(np.mean([t["net"] / t["risk"] for t in trades if t.get("risk")])), 3) if trades else None,
                 "fees": round(sum(t["fees"] for t in trades), 2), "funding": round(sum(t["funding"] for t in trades), 2),
                 "exits": pd.Series([t["exit_reason"].split(" (")[0] for t in trades]).value_counts().to_dict(),
                 "by_year": by_year}

    # The research simulator on the same window and settings, for comparison.
    p = panel({sym[:-3]: df for sym, df in coins.items()}, s.bar_hours)
    sim = run(p, Rules("research", bar_hours=s.bar_hours, risk=s.risk_per_trade, max_gross=s.max_gross_leverage,
                       stop_atr=s.stop_atr, trail=s.trail, trend_exit=s.trend_exit, target_r=s.take_profit_r or None,
                       max_hold_bars=None), start_time=start)
    research = metrics(sim, start, int(clock.t) + 1)

    report = {"generated_at": int(time.time()), "start": a.start, "fills": a.fills, "settings": {"risk_per_trade": s.risk_per_trade,
              "bar_hours": s.bar_hours, "stop_atr": s.stop_atr, "trail": s.trail, "trend_exit": s.trend_exit,
              "max_gross_leverage": s.max_gross_leverage, "daily_loss_limit": s.daily_loss_limit},
              "coins": sorted(coins), "live_code_replay": live_code, "research_simulator": research,
              "equity": [[int(t), round(float(e), 2)] for t, e in zip(times[::96], curve[::96])]}
    os.makedirs("data/research", exist_ok=True)
    with open("data/research/replay.json", "w") as f:
        json.dump(report, f)
    print(json.dumps({k: report[k] for k in ("settings", "live_code_replay", "research_simulator")}, indent=1))
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
