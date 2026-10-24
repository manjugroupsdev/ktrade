import copy

import numpy as np
import pandas as pd
import pytest

from ktrade.config import Settings
from ktrade.ledger import Ledger

NOW = 1_800_000_000.0 + 300           # 5 minutes past an hour


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def make_candles(start, drift, n=1000, end=NOW, widen_last=24, wiggle=0.0002, seed=None):
    """Hourly candles ending before `end`; the band widens over the last bars so volatility is 'expanding'."""
    t0 = int(end // 3600 * 3600) - n * 3600
    noise = np.random.default_rng(seed).standard_normal(n) if seed is not None else np.sin(np.arange(n))
    close = start * np.exp(np.linspace(0, drift, n)) * (1 + wiggle * noise)
    band = np.full(n, 0.0002)
    if widen_last:
        band[-widen_last:] = 0.004
    return pd.DataFrame({"time": t0 + np.arange(n) * 3600, "open": close, "high": close * (1 + band),
                         "low": close * (1 - band), "close": close, "volume": 10.0})


class FakeDelta:
    def __init__(self, clock):
        self.clock = clock
        old = NOW - 400 * 86400
        self.products = {
            "UPUSD": {"contract_value": 0.01, "tick": 0.01, "taker_fee": 0.0005, "launched": old},
            "DNUSD": {"contract_value": 0.01, "tick": 0.01, "taker_fee": 0.0005, "launched": old},
            "FLATUSD": {"contract_value": 0.01, "tick": 0.01, "taker_fee": 0.0005, "launched": old},
            "NEWUSD": {"contract_value": 1, "tick": 0.0001, "taker_fee": 0.0005, "launched": NOW - 86400},   # fresh listing
            "THINUSD": {"contract_value": 1, "tick": 0.0001, "taker_fee": 0.0005, "launched": old},          # low volume
        }
        self.frames = {"UPUSD": make_candles(1000, 0.4, seed=1), "DNUSD": make_candles(1000, -0.4, seed=2), "FLATUSD": make_candles(1000, 0.0, widen_last=0, wiggle=0.0),
                       "NEWUSD": make_candles(1, 3.0), "THINUSD": make_candles(1, 0.5)}
        self.quotes = {s: self._quote(f["close"].iloc[-1]) for s, f in self.frames.items()}
        self.quotes["THINUSD"]["turnover_usd"] = 100_000

    @staticmethod
    def _quote(px, turnover=50_000_000):
        return {"bid": px * 0.9999, "ask": px * 1.0001, "last": px, "turnover_usd": turnover, "funding_rate": 0.01, "ts": NOW}

    def set_price(self, sym, px):
        self.quotes[sym].update(self._quote(px, self.quotes[sym]["turnover_usd"]))

    def perpetuals(self):
        return copy.deepcopy(self.products)

    def tickers(self):
        return copy.deepcopy(self.quotes)

    def candles(self, symbol, resolution="1h", days=40):
        return self.frames[symbol].copy()


class FakeCouncil:
    def __init__(self, entries=None, exits=None, error=None):
        self.entries, self.exits, self.error, self.calls = entries or [], exits or [], error, []

    def decide(self, candidates, positions, equity):
        self.calls.append({"candidates": [c["symbol"] for c in candidates], "positions": [p["symbol"] for p in positions]})
        if self.error:
            raise self.error
        return {"market_view": "test", "entries": list(self.entries), "exits": list(self.exits), "cost_usd": 0.01}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings(tmp_path):
    # The original hourly fixed-target rules: these tests pin down stops, targets, time limits and hourly cadence.
    return Settings(db_path=str(tmp_path / "t.db"), min_turnover_usd=5_000_000, min_listing_days=60, max_candidates=5,
                    bar_hours=1, trail=False, trend_exit=False, take_profit_r=2.0, max_hold_hours=48)


@pytest.fixture
def ledger(settings, clock):
    return Ledger(settings.db_path, settings.start_equity, clock)


@pytest.fixture
def delta(clock):
    return FakeDelta(clock)
