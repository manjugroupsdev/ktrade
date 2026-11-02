"""
The Claude agent book: Claude decides everything (coin, direction, size, leverage, stop, target, exits), and this
module enforces hard caps in code before any order reaches the exchange. Claude cannot change the caps.

Every hour Claude gets the account, its open positions (with their exchange-held stops), its recent decisions and
a table of liquid coins, then answers with actions in a JSON schema. Each action passes the guard or is refused
with a reason; both are logged and shown on the dashboard. Stops live on the exchange as bracket orders, so a
sleeping Mac cannot leave a position unprotected.
"""
import json
import logging
import math
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Union

from ktrade.config import Settings
from ktrade.exchange import DEMO_URL, LIVE_URL, DeltaTrader, ExchangeError
from ktrade.scanner import scan

log = logging.getLogger("ktrade.agent")
BOOK = "agent"
LIVE_PHRASE = "I accept that I can lose this money"
FEE = 0.0005 * 1.18          # Delta taker fee + GST, per side
SLIP = 0.0005                # allowance per side for market-order slippage


# --- the guard: pure functions, unit-tested --------------------------------------------------------
@dataclass
class OpenPlan:
    symbol: str
    side: str                # long / short
    size: int                # contracts
    leverage: int
    price: float             # expected fill (ask for long, bid for short)
    stop: float
    target: Optional[float]
    risk_usd: float
    notional: float


def check_open(s: Settings, a: Dict[str, Any], ctx: Dict[str, Any]) -> Union[OpenPlan, str]:
    """A sized, capped order for an `open` action, or the reason it is refused."""
    sym, side = a.get("symbol"), a.get("side")
    if ctx.get("halted"):
        return f"halted: {ctx['halted']}"
    if sym not in ctx["allowed"]:
        return f"{sym} is not in the tradable list"
    if side not in ("long", "short"):
        return "side must be long or short"
    if any(p["symbol"] == sym for p in ctx["positions"]):
        return f"already holding {sym}"
    if len(ctx["positions"]) >= s.agent_max_positions:
        return f"already {len(ctx['positions'])} positions (max {s.agent_max_positions})"
    if ctx["entries_today"] >= s.agent_max_entries_per_day:
        return f"already {ctx['entries_today']} entries today (max {s.agent_max_entries_per_day})"
    equity = ctx["equity"]
    if ctx["day_start"] > 0 and (ctx["day_start"] - equity) / ctx["day_start"] * 100 >= s.agent_daily_loss_pct:
        return f"daily loss limit {s.agent_daily_loss_pct:g}% reached"
    q, prod = ctx["quotes"].get(sym), ctx["products"].get(sym)
    if not q or not prod:
        return f"no live price for {sym}"
    price = q["ask"] if side == "long" else q["bid"]
    try:
        stop = float(a.get("stop_price"))
    except (TypeError, ValueError):
        return "stop_price is required"
    target = a.get("take_profit_price")
    target = float(target) if target not in (None, "", 0) else None
    sign = 1 if side == "long" else -1
    if (price - stop) * sign <= 0:
        return f"stop {stop:g} is on the wrong side of the price {price:g}"
    if target is not None and (target - price) * sign <= 0:
        return f"take-profit {target:g} is on the wrong side of the price {price:g}"
    stop_pct = abs(price - stop) / price * 100
    if not s.agent_min_stop_pct <= stop_pct <= s.agent_max_stop_pct:
        return f"stop is {stop_pct:.2f}% away; allowed {s.agent_min_stop_pct:g}%-{s.agent_max_stop_pct:g}%"
    # Leverage: Claude's choice, capped, and low enough that the stop triggers well before liquidation.
    lev = int(max(1, min(float(a.get("leverage") or 1), s.agent_max_leverage, math.floor(80 / stop_pct))))
    risk_pct = max(0.0, min(float(a.get("risk_pct") or 0), s.agent_max_risk_pct))
    cv = prod["contract_value"]
    per_contract = (abs(price - stop) + (2 * FEE + 2 * SLIP) * price) * cv
    size = math.floor(equity * risk_pct / 100 / per_contract)
    size = min(size, math.floor(lev * equity / (price * cv)))                       # notional within the leverage
    size = min(size, math.floor(ctx["available"] * 0.9 * lev / (price * cv)))       # margin actually available
    if size < 1:
        return (f"too small: one contract risks ${per_contract:,.2f}, budget ${equity * risk_pct / 100:,.2f} "
                f"({risk_pct:g}%) at {lev}x")
    return OpenPlan(sym, side, size, lev, price, stop, target, round(size * per_contract, 2), round(size * price * cv, 2))


def check_move_stop(a: Dict[str, Any], ctx: Dict[str, Any]) -> Union[float, str]:
    """A new stop for an open position: it may only tighten, and must stay on the safe side of the price."""
    pos = next((p for p in ctx["positions"] if p["symbol"] == a.get("symbol")), None)
    if not pos:
        return f"no open position in {a.get('symbol')}"
    try:
        new = float(a.get("stop_price"))
    except (TypeError, ValueError):
        return "stop_price is required"
    q, sign = ctx["quotes"].get(pos["symbol"]), 1 if pos["side"] == "long" else -1
    old = (ctx["stops"].get(pos["symbol"]) or {}).get("stop")
    if old is not None and (new - old) * sign <= 0:
        return f"stops may only tighten (current {old:g}, asked {new:g})"
    price = (q["bid"] if sign > 0 else q["ask"]) if q else pos["mark"]
    if (price - new) * sign <= 0:
        return f"stop {new:g} is past the current price {price:g}; close the position instead"
    return new


# --- Claude ------------------------------------------------------------------------------------------
SYSTEM = """You are an autonomous crypto trader running a small real-or-demo account on Delta Exchange India perpetual
futures. You decide what to trade, which direction, how much risk, what leverage, where the stop is and when to exit.
Hard limits are enforced by code and cannot be changed: any action outside them is refused and logged.

Lessons from live LLM trading contests: most AI traders lost money, mainly by over-trading, so fees and noise ate
the account. Fees + slippage are roughly 0.15% of notional per round trip. Doing nothing is often the best action.
Take a trade only when trend, volatility and fresh news/context agree, with a stop at a level that proves you wrong.
Prefer letting winners run with a trailing stop over tiny targets. Never average down. Be concise and specific."""


def schema(symbols: List[str]) -> Dict[str, Any]:
    return {"type": "object", "required": ["market_view", "actions"], "properties": {
        "market_view": {"type": "string", "maxLength": 500},
        "actions": {"type": "array", "maxItems": 6, "items": {"type": "object", "required": ["type", "symbol", "reason"], "properties": {
            "type": {"type": "string", "enum": ["open", "close", "move_stop"]},
            "symbol": {"type": "string", "enum": symbols or ["NONE"]},
            "side": {"type": "string", "enum": ["long", "short"]},
            "risk_pct": {"type": "number", "minimum": 0, "maximum": 100, "description": "% of equity lost if the stop is hit"},
            "leverage": {"type": "number", "minimum": 1, "maximum": 200},
            "stop_price": {"type": "number"},
            "take_profit_price": {"type": ["number", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "maxLength": 400}}}}}}


class ClaudeAgent:
    def __init__(self, s: Settings, run: Callable = subprocess.run):
        self.s, self.run = s, run

    def decide(self, context: Dict[str, Any], symbols: List[str]) -> Dict[str, Any]:
        prompt = ("Current state (JSON):\n" + json.dumps(context, indent=1, default=str) +
                  "\n\nDecide your actions now. An empty actions list is a valid answer. Reply with the JSON schema.")
        tools = ["WebSearch"] if self.s.claude_web else []
        cmd = [self.s.claude_bin, "-p", prompt, "--model", self.s.agent_model, "--output-format", "json",
               "--json-schema", json.dumps(schema(symbols)), "--system-prompt", SYSTEM, "--no-session-persistence",
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", ""]
        cmd += ["--tools", *tools, "--allowedTools", *tools] if tools else ["--tools", ""]
        try:
            done = self.run(cmd, capture_output=True, text=True, timeout=self.s.claude_timeout)
            out = json.loads(done.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            raise ExchangeError(f"Claude did not answer: {e}") from e
        if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
            raise ExchangeError(f"Claude error: {str(out.get('result') or out)[:300]}")
        d = out["structured_output"]
        return {"market_view": str(d.get("market_view", ""))[:500],
                "actions": [x for x in d.get("actions", []) if x.get("symbol") in symbols][:6],
                "cost_usd": float(out.get("total_cost_usd") or 0)}


# --- the loop ------------------------------------------------------------------------------------------
class AgentLoop:
    def __init__(self, s: Settings, ledger, data_delta, trader: Optional[DeltaTrader] = None, claude=None, clock=time.time):
        if s.agent_mode == "live" and s.agent_live_confirm != LIVE_PHRASE:
            raise ExchangeError(f'live mode needs KTRADE_AGENT_LIVE_CONFIRM="{LIVE_PHRASE}" in .env')
        self.s, self.ledger, self.data, self.clock = s, ledger, data_delta, clock
        self.trader = trader or DeltaTrader(LIVE_URL if s.agent_mode == "live" else DEMO_URL, s.delta_key, s.delta_secret, clock=clock)
        self.claude = claude or ClaudeAgent(s)
        self.status: Dict[str, Any] = {"mode": s.agent_mode, "enabled": True}
        self._snap_at = 0.0

    # state ------------------------------------------------------------------------------------------
    def _day(self) -> str:
        from ktrade.engine import IST
        return datetime.fromtimestamp(self.clock(), IST).strftime("%Y-%m-%d")

    def context(self) -> Dict[str, Any]:
        acct, quotes, products = self.trader.equity(), self.trader.quotes(), self.trader.products()
        positions, stops = self.trader.positions(), self.trader.stop_orders()
        start = float(self.ledger.get("agent_start_equity") or 0) or acct["equity"]
        if not self.ledger.get("agent_start_equity"):
            self.ledger.put("agent_start_equity", str(acct["equity"]))
        day = self._day()
        return {"equity": acct["equity"], "available": acct["available"], "start_equity": start,
                "day_start": self.ledger.day_start_equity(BOOK, day, acct["equity"]),
                "entries_today": int(self.ledger.get(f"agent_entries_{day}", "0")),
                "positions": positions, "stops": stops, "quotes": quotes, "products": products,
                "halted": self.ledger.get("agent_halted") or None, "allowed": set()}

    def tradable(self, ctx: Dict[str, Any], real: Dict[str, Dict[str, float]]) -> List[str]:
        """Liquid coins whose exchange price agrees with the real market (demo prices can drift on thin books)."""
        out = []
        for sym, q in ctx["quotes"].items():
            r = real.get(sym)
            if sym not in ctx["products"] or not r or r["turnover_usd"] < self.s.min_turnover_usd:
                continue
            mid, rmid = (q["bid"] + q["ask"]) / 2, (r["bid"] + r["ask"]) / 2
            if (q["ask"] - q["bid"]) / mid * 1e4 > self.s.max_spread_bps or abs(mid / rmid - 1) * 100 > self.s.agent_max_venue_deviation_pct:
                continue
            out.append(sym)
        return out

    def tick(self) -> Dict[str, Any]:
        """Every 30 s: refresh the account for the dashboard, enforce the floor, and run the hourly decision."""
        ctx = self.context()
        if ctx["equity"] <= self.s.agent_floor_equity and not ctx["halted"]:
            self.halt(ctx, f"equity ${ctx['equity']:,.2f} reached the ${self.s.agent_floor_equity:,.0f} floor")
            ctx = self.context()
        if self.clock() - self._snap_at >= 300:
            self._snap_at = self.clock()
            self.ledger.snapshot(BOOK, ctx["equity"])
        period = self.s.agent_cycle_minutes * 60
        slot = int(self.clock() // period)
        if not ctx["halted"] and int(self.ledger.get("agent_last_slot", "-1")) != slot and self.clock() % period >= 150:
            self.ledger.put("agent_last_slot", str(slot))
            self.cycle(ctx)
            ctx = self.context()
        self.status.update(mode=self.s.agent_mode, live=self.trader.is_live, last_tick=self.clock(), equity=ctx["equity"],
                           available=ctx["available"], start_equity=ctx["start_equity"], day_start=ctx["day_start"],
                           entries_today=ctx["entries_today"], halted=ctx["halted"], next_cycle=(slot + 1) * period + 150,
                           positions=[p | ctx["stops"].get(p["symbol"], {}) for p in ctx["positions"]],
                           caps={"risk_pct": self.s.agent_max_risk_pct, "leverage": self.s.agent_max_leverage,
                                 "positions": self.s.agent_max_positions, "entries_per_day": self.s.agent_max_entries_per_day,
                                 "daily_loss_pct": self.s.agent_daily_loss_pct, "floor": self.s.agent_floor_equity})
        return self.status

    def halt(self, ctx: Dict[str, Any], reason: str) -> None:
        log.error("AGENT HALT: %s; closing everything", reason)
        for p in ctx["positions"]:
            try:
                self.trader.close(p["symbol"])
            except ExchangeError as e:
                log.error("close %s during halt failed: %s", p["symbol"], e)
        self.ledger.put("agent_halted", reason)
        self.ledger.decide(BOOK, "*", "halt", 1, "halted", reason)

    # decisions --------------------------------------------------------------------------------------------
    def cycle(self, ctx: Dict[str, Any]) -> None:
        real = self.data.tickers()
        scan_s = replace(self.s, bar_hours=1, max_candidates=12)
        result = scan(self.data, scan_s, self.clock(), None, real)
        ctx["allowed"] = set(self.tradable(ctx, real))
        candidates = [c for c in result["candidates"] if c["symbol"] in ctx["allowed"]]
        for sym in ctx["allowed"] - {c["symbol"] for c in candidates}:      # tradable coins outside the top list
            candidates.append({"symbol": sym, "note": "tradable, not a top scanner pick"})
        recent = [{"at": datetime.fromtimestamp(d["at"]).strftime("%d %b %H:%M"), "symbol": d["symbol"], "action": d["action"],
                   "outcome": d["outcome"]} for d in self.ledger.decisions(80) if d["book"] == BOOK][:15]
        state = {
            "time_utc": datetime.utcfromtimestamp(self.clock()).strftime("%Y-%m-%d %H:%M"),
            "account": {"equity_usd": round(ctx["equity"], 2), "available_usd": round(ctx["available"], 2),
                        "started_with_usd": round(ctx["start_equity"], 2), "today_pnl_usd": round(ctx["equity"] - ctx["day_start"], 2),
                        "mode": "LIVE real money" if self.trader.is_live else "DEMO (paper)"},
            "hard_limits": {"max_risk_per_trade_pct": self.s.agent_max_risk_pct, "max_leverage": self.s.agent_max_leverage,
                            "max_positions": self.s.agent_max_positions, "entries_left_today": max(0, self.s.agent_max_entries_per_day - ctx["entries_today"]),
                            "daily_loss_stop_pct": self.s.agent_daily_loss_pct, "account_floor_usd": self.s.agent_floor_equity,
                            "stop_distance_pct": [self.s.agent_min_stop_pct, self.s.agent_max_stop_pct]},
            "open_positions": [{k: p.get(k) for k in ("symbol", "side", "size", "entry", "mark", "unrealized", "liquidation")}
                               | ctx["stops"].get(p["symbol"], {}) for p in ctx["positions"]],
            "candidates_1h": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in c.items() if k != "contract_value"} for c in candidates],
            "skipped_as_too_wild": [w["symbol"] for w in result["too_wild"]],
            "your_recent_decisions": recent,
        }
        symbols = sorted(ctx["allowed"] | {p["symbol"] for p in ctx["positions"]})
        try:
            d = self.claude.decide(state, symbols)
        except ExchangeError as e:
            log.error("agent: %s", e)
            self.ledger.decide(BOOK, "*", "error", 0, "Claude unavailable", str(e))
            self.status["claude"] = {"at": self.clock(), "error": str(e)}
            return
        self.status["claude"] = {"at": self.clock(), "market_view": d["market_view"], "actions": d["actions"], "cost_usd": d["cost_usd"]}
        log.info("agent view: %s | %d action(s)", d["market_view"][:160], len(d["actions"]))
        for a in d["actions"]:
            self.act(a, ctx)
            ctx.update({k: v for k, v in self.context().items() if k != "allowed"})

    def act(self, a: Dict[str, Any], ctx: Dict[str, Any]) -> None:
        kind, sym = a.get("type"), a.get("symbol")
        try:
            if kind == "close":
                self.trader.close(sym)
                self.ledger.decide(BOOK, sym, "close", a.get("confidence") or 0, "closed", a)
                log.info("agent closed %s: %s", sym, a.get("reason"))
            elif kind == "move_stop":
                new = check_move_stop(a, ctx)
                if isinstance(new, str):
                    self.ledger.decide(BOOK, sym, "move_stop", 0, f"refused: {new}", a)
                    return
                old = ctx["stops"].get(sym, {})
                self.trader.cancel_all(sym)
                try:
                    self.trader.protect(sym, new, old.get("target"))
                except ExchangeError:
                    try:
                        self.trader.protect(sym, old["stop"], old.get("target"))
                    except (ExchangeError, KeyError):
                        self.trader.close(sym)                            # never leave a position without a stop
                    raise
                self.ledger.decide(BOOK, sym, "move_stop", 0, f"stop moved to {new:g}", a)
            elif kind == "open":
                plan = check_open(self.s, a, ctx)
                if isinstance(plan, str):
                    self.ledger.decide(BOOK, sym, f"open {a.get('side')}", a.get("confidence") or 0, f"refused: {plan}", a)
                    log.info("agent refused %s %s: %s", a.get("side"), sym, plan)
                    return
                self.trader.set_leverage(sym, plan.leverage)
                self.trader.market(sym, "buy" if plan.side == "long" else "sell", plan.size)
                try:
                    self.trader.protect(sym, plan.stop, plan.target)
                except ExchangeError as e:
                    self.trader.close(sym)
                    self.ledger.decide(BOOK, sym, f"open {plan.side}", a.get("confidence") or 0,
                                       f"closed again: exchange refused the stop ({e})", a)
                    return
                day = self._day()
                self.ledger.put(f"agent_entries_{day}", str(int(self.ledger.get(f"agent_entries_{day}", "0")) + 1))
                self.ledger.decide(BOOK, sym, f"open {plan.side}", a.get("confidence") or 0,
                                   f"opened {plan.size} contracts at {plan.leverage}x, risk ${plan.risk_usd}, stop {plan.stop:g}"
                                   + (f", target {plan.target:g}" if plan.target else ""), a)
                log.info("agent opened %s %d %s at %dx | stop %g | target %s | risk $%.2f | %s", plan.side, plan.size, sym,
                         plan.leverage, plan.stop, plan.target, plan.risk_usd, a.get("reason", "")[:120])
        except ExchangeError as e:
            self.ledger.decide(BOOK, sym, kind, 0, f"exchange error: {e}", a)
            log.error("agent %s %s failed: %s", kind, sym, e)


def reset_halt(ledger) -> None:
    ledger.put("agent_halted", "")
