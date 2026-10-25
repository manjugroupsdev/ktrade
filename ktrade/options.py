"""
Options book: the trend rules' BTC/ETH signals, expressed by buying options instead of perpetuals.

When the trend score of BTC or ETH is strong (the same threshold as the rules book), buy the at-the-money call
(uptrend) or put (downtrend) expiring about three weeks out. The premium paid is capped at the risk budget and is
the most the trade can lose; there is no stop. Sell when the trend no longer supports the position, or a few days
before expiry. Buys pay the ask and sells receive the bid, so the real spread cost is in every result.

Not backtested: there is no free history of option prices. This book exists to be measured forward in paper.
"""
import logging
import math
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from ktrade import risk
from ktrade.config import Settings
from ktrade.delta import DeltaError
from ktrade.scanner import history_days
from ktrade.signals import features

log = logging.getLogger("ktrade.options")
BOOK = "options"


class OptionsBook:
    def __init__(self, s: Settings, delta, ledger, clock):
        self.s, self.delta, self.ledger, self.clock = s, delta, ledger, clock
        self.last: Dict[str, Any] = {}

    def fee_rate(self, premium: float, spot: float) -> float:
        """Per-side fee as a fraction of premium: underlying-notional fee, capped at a share of the premium."""
        if premium <= 0:
            return self.s.options_fee_cap
        return min(self.s.options_fee_rate * spot / premium, self.s.options_fee_cap)

    def marks(self) -> Dict[str, Dict[str, float]]:
        """Bid/ask for every option the book holds, in the engine's quote format."""
        out = {}
        for p in self.ledger.positions(BOOK):
            try:
                q = self.delta.option_quote(p["symbol"])
            except DeltaError as e:
                log.warning("option quote %s: %s", p["symbol"], e)
                continue
            # No bid means nobody will buy it: its value is zero, not the last price.
            out[p["symbol"]] = {"bid": q["bid"], "ask": q["ask"] or q["mark"], "last": q["mark"], "turnover_usd": 0.0,
                                "funding_rate": 0.0, "ts": self.clock(), "spot": q["spot"], "expiry": q["expiry"]}
        return out

    def pick(self, underlying: str, kind: str) -> Optional[Dict[str, Any]]:
        now = self.clock()
        chain = [o for o in self.delta.option_chain(underlying) if o["kind"] == kind and o["bid"] > 0 and o["ask"] > 0
                 and self.s.options_min_days <= (o["expiry"] - now) / 86400 <= self.s.options_max_days]
        chain = [o for o in chain if (o["ask"] - o["bid"]) / ((o["ask"] + o["bid"]) / 2) <= self.s.options_max_spread]
        if not chain:
            return None
        target = now + self.s.options_target_days * 86400
        expiry = min({o["expiry"] for o in chain}, key=lambda e: abs(e - target))
        return min((o for o in chain if o["expiry"] == expiry), key=lambda o: abs(o["strike"] - o["spot"]))

    def cycle(self, marks: Dict[str, Dict[str, float]]) -> None:
        """At each bar close: exit finished trades, then buy options on fresh BTC/ETH trend signals."""
        trends = {}
        res = f"{self.s.bar_hours}h" if self.s.bar_hours < 24 else "1d"
        for u in self.s.options_underlyings:
            try:
                df = self.delta.candles(f"{u}USD", res, days=history_days(self.s.bar_hours))
                trends[u] = features(df, self.s.bar_hours)["trend"] if len(df) > 200 else None
            except DeltaError as e:
                log.warning("options trend %s: %s", u, e)
                trends[u] = None
        self.last = {"at": self.clock(), "trends": trends}

        for p in self.ledger.positions(BOOK):
            u, kind = p["symbol"].split("-")[1], "call" if p["symbol"].startswith("C-") else "put"
            q = marks.get(p["symbol"])
            if not q:
                continue
            days_left = (q["expiry"] - self.clock()) / 86400
            t = trends.get(u)
            reason = (f"{days_left:.1f} days to expiry" if days_left <= self.s.options_exit_days else
                      f"trend over ({t:+.2f})" if t is not None and (t if kind == "call" else -t) <= 0 else None)
            if reason:
                r = self.ledger.close(p, q["bid"], self.fee_rate(q["bid"] or q["last"], q["spot"]), reason)
                log.info("[options] sold %s: %s, net $%.2f", p["symbol"], reason, r["net"])

        equity = self.ledger.equity(BOOK, marks)
        from datetime import datetime
        from ktrade.engine import IST
        day_start = self.ledger.day_start_equity(BOOK, datetime.fromtimestamp(self.clock(), IST).strftime("%Y-%m-%d"), equity)
        held = {p["symbol"].split("-")[1] for p in self.ledger.positions(BOOK)}
        for u, t in trends.items():
            if t is None or abs(t) < self.s.baseline_trend:
                continue
            kind, sym = ("call" if t > 0 else "put"), f"{u}USD"
            if u in held:
                continue
            if risk.daily_loss_hit(self.s, day_start, equity):
                self.ledger.decide(BOOK, sym, kind, abs(t), "refused: daily loss limit", {"thesis": f"trend {t:+.2f}"})
                continue
            try:
                o = self.pick(u, kind)
            except DeltaError as e:
                log.warning("option chain %s: %s", u, e)
                continue
            if not o:
                self.ledger.decide(BOOK, sym, kind, abs(t), "refused: no liquid option in the expiry window", {"thesis": f"trend {t:+.2f}"})
                continue
            fee = self.fee_rate(o["ask"], o["spot"])
            per_contract = o["ask"] * o["contract_value"] * (1 + fee)
            qty = math.floor(equity * self.s.risk_per_trade / per_contract)
            thesis = (f"trend {t:+.2f}: buy {kind} {o['symbol']} ({(o['expiry'] - self.clock()) / 86400:.0f} days, "
                      f"strike {o['strike']:g} vs spot {o['spot']:.0f}, IV {o['iv'] * 100:.0f}%)")
            if qty < 1:
                self.ledger.decide(BOOK, o["symbol"], kind, abs(t), f"refused: one contract costs ${per_contract:,.2f}, "
                                   f"above the ${equity * self.s.risk_per_trade:,.2f} budget", {"thesis": thesis})
                continue
            plan = SimpleNamespace(symbol=o["symbol"], side=1, qty=qty, stop=-1.0, target=None,
                                   contract_value=o["contract_value"], risk_usd=round(qty * per_contract, 2))
            did = self.ledger.decide(BOOK, o["symbol"], kind, abs(t), "opened", {"thesis": thesis})
            self.ledger.open(BOOK, plan, o["ask"], fee, 0.0, thesis, did)
            marks[o["symbol"]] = {"bid": o["bid"], "ask": o["ask"], "last": o["mark"], "funding_rate": 0.0,
                                  "turnover_usd": 0.0, "ts": self.clock(), "spot": o["spot"], "expiry": o["expiry"]}
            log.info("[options] bought %d %s @ %.2f | premium at risk $%.2f | %s", qty, o["symbol"], o["ask"],
                     plan.risk_usd, thesis)
            held.add(u)
