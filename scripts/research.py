"""
Reproducible research for the rules-only book.

    .venv/bin/python scripts/research.py          # downloads/caches history, writes data/research/report.json

Variants are fixed in advance. The pick is made on 2021-2023 only and then checked on 2024 onwards,
which the pick never saw. Results are written as JSON (for the dashboard) and printed as a table.
"""
import json
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ktrade.backtest import Rules, buy_and_hold, metrics, panel, run    # noqa: E402
from ktrade.history import UNIVERSE, hourly                             # noqa: E402

IN_SAMPLE = (int(pd.Timestamp("2021-01-01", tz="UTC").timestamp()), int(pd.Timestamp("2024-01-01", tz="UTC").timestamp()))
OUT_SAMPLE = (IN_SAMPLE[1], int(time.time()))

VARIANTS = [
    Rules("live v0.1: 1h, 2R target, 48h limit"),
    Rules("1h trailing stop, exit on trend loss", trail=True, target_r=None, max_hold_bars=None, trend_exit=True),
    Rules("4h, 2R target, 8-day limit", bar_hours=4),
    Rules("4h trailing stop, exit on trend loss", bar_hours=4, trail=True, target_r=None, max_hold_bars=None, trend_exit=True),
    Rules("4h trailing, long only", bar_hours=4, trail=True, target_r=None, max_hold_bars=None, trend_exit=True, long_only=True),
    Rules("daily trailing stop, exit on trend loss", bar_hours=24, trail=True, target_r=None, max_hold_bars=None, trend_exit=True),
    Rules("daily trailing, long only", bar_hours=24, trail=True, target_r=None, max_hold_bars=None, trend_exit=True, long_only=True),
]


def main() -> None:
    t0 = time.time()
    data = {c: hourly(c) for c in UNIVERSE}
    panels = {}
    rows = []
    for r in VARIANTS:
        p = panels.setdefault(r.bar_hours, panel(data, r.bar_hours))
        res = run(p, r)
        rows.append({"name": r.name, "rules": r.__dict__, "in_sample": metrics(res, *IN_SAMPLE),
                     "out_of_sample": metrics(res, *OUT_SAMPLE),
                     "equity": [[int(t), round(float(e), 2)] for t, e in zip(res["time"][::24], res["equity"][::24]) if e == e]})
        print(f"done {r.name} ({time.time() - t0:.0f}s)", flush=True)
    p = panels[24] if 24 in panels else panel(data, 24)
    bench = {"BTC buy & hold": {"in_sample": buy_and_hold(p, ["BTC"], *IN_SAMPLE), "out_of_sample": buy_and_hold(p, ["BTC"], *OUT_SAMPLE)},
             "20-coin equal-weight buy & hold": {"in_sample": buy_and_hold(p, UNIVERSE, *IN_SAMPLE),
                                                 "out_of_sample": buy_and_hold(p, UNIVERSE, *OUT_SAMPLE)}}

    best = max(rows, key=lambda x: (x["in_sample"].get("sharpe") or -9))
    report = {"generated_at": int(time.time()), "universe": UNIVERSE, "in_sample": "2021-01-01 to 2023-12-31",
              "out_of_sample": "2024-01-01 to now", "picked_on_in_sample": best["name"], "variants": rows, "benchmarks": bench,
              "costs": "taker 0.05% + 18% GST and 3 bps slippage per side, funding 0.01%/8h (longs pay)",
              "caveats": ["Survivor coins only (no LUNA/FTT): long-biased results look better than reality.",
                          "Binance spot prices stand in for Delta perpetuals.",
                          "Fractional quantities; live trading rounds to whole contracts.",
                          "One fixed set of variants; the out-of-sample period was not used to choose."]}
    os.makedirs("data/research", exist_ok=True)
    with open("data/research/report.json", "w") as f:
        json.dump(report, f)

    def fmt(m):
        return (f"ret {m.get('return_pct', 0):>7.1f}%  CAGR {str(m.get('cagr_pct')):>6}%  DD {m.get('max_drawdown_pct', 0):>6.1f}%  "
                f"Sharpe {m.get('sharpe', 0):>5.2f}  PF {str(m.get('profit_factor')):>5}  trades {m.get('trades', 0):>4}") if m else "-"
    print(f"\n{'variant':<42}| IN-SAMPLE 2021-2023 (used to pick)                                   | OUT-OF-SAMPLE 2024-now (never seen)")
    for x in rows:
        print(f"{x['name']:<42}| {fmt(x['in_sample'])} | {fmt(x['out_of_sample'])}")
    for k, v in bench.items():
        print(f"{k:<42}| ret {v['in_sample']['return_pct']:>7.1f}%  DD {v['in_sample']['max_drawdown_pct']:>6.1f}%"
              f"{'':<40}| ret {v['out_of_sample']['return_pct']:>7.1f}%  DD {v['out_of_sample']['max_drawdown_pct']:>6.1f}%")
    print(f"\npicked on in-sample Sharpe: {best['name']}")
    for x in rows:
        print(f"  {x['name']:<42} by year: " + " ".join(f"{y}:{v:+.0f}%" for y, v in {**x['in_sample'].get('by_year', {}), **x['out_of_sample'].get('by_year', {})}.items()))


if __name__ == "__main__":
    main()
