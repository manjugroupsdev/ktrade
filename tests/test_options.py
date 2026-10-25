from dataclasses import replace

import pytest

from ktrade.engine import Engine
from ktrade.options import BOOK
from conftest import NOW, FakeCouncil, make_candles

DAY = 86400


class OptionsDelta:
    """Perp candles for BTC/ETH plus a small option chain whose quotes the test can move."""

    def __init__(self, clock, btc_drift=0.4):
        self.clock = clock
        self.frames = {"BTCUSD": make_candles(80000, btc_drift, seed=3), "ETHUSD": make_candles(2300, 0.0, widen_last=0, wiggle=0.0)}
        spot = self.frames["BTCUSD"]["close"].iloc[-1]
        self.spot = spot
        self.options = {}
        for days, tag in ((5, "A"), (21, "B"), (60, "C")):
            for strike in (round(spot, -3) - 2000, round(spot, -3), round(spot, -3) + 2000):
                for kind in ("C", "P"):
                    sym = f"{kind}-BTC-{int(strike)}-{tag}"
                    self.options[sym] = {"symbol": sym, "kind": "call" if kind == "C" else "put", "underlying": "BTC",
                                         "strike": float(strike), "expiry": NOW + days * DAY, "bid": 1900.0, "ask": 2000.0,
                                         "mark": 1950.0, "spot": spot, "contract_value": 0.001, "delta": 0.5, "iv": 0.4}

    def tickers(self):
        return {}

    def perpetuals(self):
        return {}

    def candles(self, symbol, resolution="4h", days=100):
        return self.frames[symbol].copy()

    def option_chain(self, underlying):
        return [dict(o) for o in self.options.values() if o["underlying"] == underlying]

    def option_quote(self, symbol):
        return dict(self.options[symbol])


def make(settings, ledger, clock, delta):
    s = replace(settings, bar_hours=4, options_enabled=True, risk_per_trade=0.02, options_underlyings=("BTC",),
                max_hold_hours=0.0, take_profit_r=0.0)
    return Engine(s, delta, ledger, FakeCouncil(), clock)


def test_buys_the_at_the_money_call_about_three_weeks_out_within_budget(settings, ledger, clock):
    d = OptionsDelta(clock)
    e = make(settings, ledger, clock, d)
    e.tick(force_cycle=True)
    (p,) = ledger.positions(BOOK)
    assert p["symbol"] == f"C-BTC-{int(round(d.spot, -3))}-B"                  # call, 21 days, nearest strike
    fee = 0.0001 * 1.18 * d.spot / 2000.0
    assert p["qty"] == int(20 // (2.0 * (1 + fee))) == 9 and p["entry"] == 2000.0   # $20 budget / ($2 + fee) per contract
    assert p["qty"] * 2000.0 * 0.001 <= 1000 * 0.02


def test_no_purchase_when_the_trend_is_weak(settings, ledger, clock):
    d = OptionsDelta(clock, btc_drift=0.0)
    d.frames["BTCUSD"] = make_candles(80000, 0.0, widen_last=0, wiggle=0.0)
    e = make(settings, ledger, clock, d)
    e.tick(force_cycle=True)
    assert ledger.positions(BOOK) == []


def test_profit_is_premium_change_and_option_sells_before_expiry(settings, ledger, clock):
    d = OptionsDelta(clock)
    e = make(settings, ledger, clock, d)
    e.tick(force_cycle=True)
    (p,) = ledger.positions(BOOK)
    d.options[p["symbol"]].update(bid=5900.0, ask=6000.0)                    # the trend paid off: premium tripled
    clock.t += 19 * DAY                                                        # two days left
    e.tick(force_cycle=True)
    (t,) = ledger.trades(BOOK)
    assert "days to expiry" in t["exit_reason"]
    assert t["gross"] == pytest.approx((5900 - 2000) * 0.001 * 9)
    assert t["net"] < t["gross"] and t["net"] > 34


def test_worst_case_loss_is_the_premium(settings, ledger, clock):
    d = OptionsDelta(clock)
    e = make(settings, ledger, clock, d)
    e.tick(force_cycle=True)
    (p,) = ledger.positions(BOOK)
    d.options[p["symbol"]].update(bid=0.0, ask=5.0, mark=0.0)               # worthless
    clock.t += 19 * DAY
    e.tick(force_cycle=True)
    (t,) = ledger.trades(BOOK)
    premium = p["qty"] * 2000.0 * 0.001
    assert -(premium + 0.5) <= t["net"] <= -premium                            # lost the premium plus fees, nothing more
    assert -t["net"] <= 1000 * 0.02                                            # within the 2% risk budget
