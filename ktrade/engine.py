"""
The trading loop.

Every tick (30 s): mark both books to live prices and close positions that hit their stop (or optional target /
time limit). After each bar closes (every 4 hours by default): trail the stops, exit positions whose trend has
ended, scan, let the baseline rules and Claude each decide on the same candidates, and open whatever the risk
engine accepts. Exit rules are identical for both books. Every decision, including refusals, is recorded.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ktrade import risk
from ktrade.config import Settings
from ktrade.council import CouncilError
from ktrade.ledger import BOOKS, Ledger
from ktrade.scanner import correlation, history_days, scan
from ktrade.signals import features

IST = timezone(timedelta(hours=5, minutes=30))
log = logging.getLogger("ktrade")


class Engine:
    def __init__(self, s: Settings, delta, ledger: Ledger, council, clock=time.time):
        self.s, self.delta, self.ledger, self.council, self.clock = s, delta, ledger, council, clock
        self.products: Optional[Dict] = None
        self._products_at = 0.0
        self._snapshot_at = 0.0
        self.returns: Dict = {}
        self.status: Dict[str, Any] = {"started_at": clock(), "last_tick": None, "last_cycle": None,
                                       "scan": None, "council": None, "last_error": None}

    # --- helpers -----------------------------------------------------------------------------
    def _products(self) -> Dict:
        if self.products is None or self.clock() - self._products_at > 6 * 3600:
            self.products, self._products_at = self.delta.perpetuals(), self.clock()
        return self.products

    def _fill(self, quote: Dict[str, float], side: int, opening: bool) -> float:
        """Market order fill: pay the spread and slippage in the adverse direction."""
        buying = side > 0 if opening else side < 0
        px = quote["ask"] if buying else quote["bid"]
        return px * (1 + self.s.slippage_bps / 1e4) if buying else px * (1 - self.s.slippage_bps / 1e4)

    def positions(self, marks: Dict, book: Optional[str] = None) -> List[Dict[str, Any]]:
        out = []
        for p in self.ledger.positions(book):
            t = marks.get(p["symbol"])
            out.append(p | {"side": "long" if p["side"] > 0 else "short", "side_sign": p["side"],
                            "mark": (t["bid"] if p["side"] > 0 else t["ask"]) if t else None,
                            "unrealized": round(self.ledger.unrealized(p, marks), 2),
                            "hours_open": round((self.clock() - p["opened_at"]) / 3600, 1)})
        return out

    # --- loop --------------------------------------------------------------------------------
    def tick(self, force_cycle: bool = False) -> Dict[str, Any]:
        marks = self.delta.tickers()
        self.manage_exits(marks)
        bar_sec = self.s.bar_hours * 3600
        bar = int(self.clock() // bar_sec)
        due = int(self.ledger.get(f"last_cycle_bar_{self.s.bar_hours}h", "-1")) != bar and self.clock() % bar_sec >= 120
        self.status["next_cycle"] = (bar + 1) * bar_sec + 120
        if due or force_cycle:
            self.ledger.put(f"last_cycle_bar_{self.s.bar_hours}h", str(bar))   # set first: a failing cycle is not hammered
            self.cycle(marks)
        if self.clock() - self._snapshot_at >= 900:
            self._snapshot_at = self.clock()
            for b in BOOKS:
                self.ledger.snapshot(b, self.ledger.equity(b, marks))
        day = datetime.fromtimestamp(self.clock(), IST).strftime("%Y-%m-%d")
        books = {}
        for b in BOOKS:
            books[b] = self.ledger.summary(b, marks)
            books[b]["today_pnl"] = round(books[b]["equity"] - self.ledger.day_start_equity(b, day, books[b]["equity"]), 2)
        self.status.update(last_tick=self.clock(), books=books, positions=self.positions(marks),
                           claude_cost_today=round(float(self.ledger.get(f"claude_cost_{day}", "0")), 3))
        return self.status

    def manage_exits(self, marks: Dict) -> List[Dict[str, Any]]:
        closed = []
        for p in self.ledger.positions():
            t = marks.get(p["symbol"])
            if not t:
                continue
            px = t["bid"] if p["side"] > 0 else t["ask"]
            reason = ("stop" if (px - p["stop"]) * p["side"] <= 0 else
                      "target" if p["target"] is not None and (px - p["target"]) * p["side"] >= 0 else
                      "time limit" if self.s.max_hold_hours and (self.clock() - p["opened_at"]) / 3600 >= self.s.max_hold_hours
                      else None)
            if reason:
                closed.append(self.ledger.close(p, self._fill(t, p["side"], opening=False), self.s.taker_fee, reason))
                log.info("[%s] closed %s %s: %s, net $%.2f", p["book"], "long" if p["side"] > 0 else "short",
                         p["symbol"], reason, closed[-1]["net"])
        return closed

    def review_positions(self, marks: Dict) -> None:
        """At each bar close: ratchet trailing stops and close positions whose trend has ended (both books alike)."""
        res = f"{self.s.bar_hours}h" if self.s.bar_hours < 24 else "1d"
        cache: Dict[str, Optional[Dict]] = {}
        for p in self.ledger.positions():
            sym, side = p["symbol"], p["side"]
            if sym not in cache:
                try:
                    df = self.delta.candles(sym, res, days=history_days(self.s.bar_hours))
                    cache[sym] = features(df, self.s.bar_hours) if len(df) > 200 else None
                except Exception as e:                                # keep the old stop; it is still enforced every tick
                    log.warning("review %s: %s", sym, e)
                    cache[sym] = None
            f, t = cache[sym], marks.get(sym)
            if not f or not t:
                continue
            if self.s.trend_exit and f["trend"] * side <= 0:
                r = self.ledger.close(p, self._fill(t, side, opening=False), self.s.taker_fee, f"trend over ({f['trend']:+.2f})")
                log.info("[%s] closed %s: trend over, net $%.2f", p["book"], sym, r["net"])
                continue
            if self.s.trail:
                new = f["price"] - side * self.s.stop_atr * f["atr"]
                if (new - p["stop"]) * side > 0:
                    self.ledger.update_stop(p["id"], new)
                    log.info("[%s] %s trailing stop moved %.6g -> %.6g", p["book"], sym, p["stop"], new)

    def cycle(self, marks: Dict) -> None:
        self.review_positions(marks)
        products = self._products()
        result = scan(self.delta, self.s, self.clock(), products, marks)
        cands, self.returns = result["candidates"], result["returns"]
        by_sym = {c["symbol"]: c for c in cands}
        self.status.update(last_cycle=self.clock(), scan={"universe": result["universe"], "candidates": cands,
                                                         "too_wild": result["too_wild"],
                                                         "duplicates": result["duplicates"]})
        log.info("scan: %d liquid coins, candidates %s", len(result["universe"]),
                 ", ".join(f"{c['symbol']} trend {c['trend']:+.2f} vol x{c['vol_ratio']:.2f}" for c in cands) or "none")

        th = self.s.baseline_trend
        self.enter("baseline", [{"symbol": c["symbol"], "conviction": abs(c["trend"]),
                                 "action": "long" if c["trend"] >= th else "short" if c["trend"] <= -th else "skip",
                                 "thesis": f"rules: trend {c['trend']:+.2f}, volatility x{c['vol_ratio']:.2f}"} for c in cands],
                   by_sym, marks)

        held = self.positions(marks, "ai")
        if not cands and not held:
            return
        try:
            d = self.council.decide(cands, held, self.ledger.equity("ai", marks))
        except CouncilError as e:
            log.error("Claude council unavailable: %s (AI book takes no new trades this hour)", e)
            self.ledger.decide("ai", "*", "error", 0, "council unavailable", str(e))
            self.status["council"] = {"at": self.clock(), "error": str(e)}
            return
        self.status["council"] = {"at": self.clock(), "market_view": d["market_view"], "entries": d["entries"],
                                  "exits": d["exits"], "cost_usd": d.get("cost_usd")}
        key = f"claude_cost_{datetime.fromtimestamp(self.clock(), IST).strftime('%Y-%m-%d')}"
        self.ledger.put(key, str(float(self.ledger.get(key, "0")) + float(d.get("cost_usd") or 0)))
        for x in d["exits"]:
            if x["action"] != "exit":
                continue
            for p in self.ledger.positions("ai"):
                if p["symbol"] == x["symbol"] and marks.get(p["symbol"]):
                    r = self.ledger.close(p, self._fill(marks[p["symbol"]], p["side"], opening=False), self.s.taker_fee,
                                          f"Claude exit: {x['reason']}"[:200])
                    self.ledger.decide("ai", p["symbol"], "exit", 1, "closed", x)
                    log.info("[ai] Claude closed %s: %s, net $%.2f", p["symbol"], x["reason"], r["net"])
        self.enter("ai", d["entries"], by_sym, marks, min_conviction=self.s.min_conviction)

    def enter(self, book: str, decisions: List[Dict], by_sym: Dict, marks: Dict, min_conviction: float = 0.0) -> None:
        equity = self.ledger.equity(book, marks)
        day = datetime.fromtimestamp(self.clock(), IST).strftime("%Y-%m-%d")
        day_start = self.ledger.day_start_equity(book, day, equity)
        for d in sorted(decisions, key=lambda x: -x["conviction"]):
            sym, act, conv = d["symbol"], d["action"], d["conviction"]
            if act not in ("long", "short") or conv < min_conviction:
                self.ledger.decide(book, sym, act, conv, "skip", d)
                continue
            held, t, c, side = self.ledger.positions(book), marks.get(sym), by_sym.get(sym), 1 if act == "long" else -1
            reason, plan, fill = None, None, None
            if any(p["symbol"] == sym for p in held):
                reason = "already holding it"
            elif len(held) >= self.s.max_positions:
                reason = f"already {len(held)} positions (max {self.s.max_positions})"
            elif risk.daily_loss_hit(self.s, day_start, self.ledger.equity(book, marks)):
                reason = f"daily loss limit {self.s.daily_loss_limit:.0%} reached"
            elif not t or not c:
                reason = "no live price"
            elif twin := next((p["symbol"] for p in held
                               if correlation(self.returns, sym, p["symbol"]) * side * p["side"] > self.s.max_correlation), None):
                reason = f"same bet as the open {twin} position"
            else:
                fill = self._fill(t, side, opening=True)
                gross = sum(p["qty"] * p["contract_value"] * p["entry"] for p in held)
                plan = risk.plan_entry(self.s, sym, side, fill, c["atr"], c["contract_value"], equity, gross)
                if isinstance(plan, str):
                    reason = plan
            if reason:
                self.ledger.decide(book, sym, act, conv, f"refused: {reason}", d)
                continue
            did = self.ledger.decide(book, sym, act, conv, "opened", d)
            self.ledger.open(book, plan, fill, self.s.taker_fee, t["funding_rate"], d.get("thesis", "")[:200], did)
            log.info("[%s] opened %s %d %s @ %.6g | stop %.6g | target %s | risk $%.2f | %s", book, act, plan.qty, sym,
                     fill, plan.stop, f"{plan.target:.6g}" if plan.target else "none (trailing)", plan.risk_usd,
                     d.get("thesis", "")[:120])
