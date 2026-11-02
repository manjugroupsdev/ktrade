"""
The Claude agent book: Claude decides everything (coin, direction, size, leverage, stop, target, exits), and this
module enforces hard caps in code before any order reaches the exchange. Claude cannot change the caps.

Three Claude roles, each a separate headless call with its own job:
  news desk     every 30 min, searches the web (crypto, SEC/regulation, macro and inflation, war and politics,
                equities) and writes a brief; it can wake the trader early when something big breaks
  trader        every hour (or when woken), reads the brief, the account, its positions and a table of liquid
                coins, then answers with actions in a JSON schema
  risk watcher  when a held coin moves sharply within 15 minutes, finds out why and may close or tighten the stop
                (it can never open a trade)
 Each action passes the guard or is refused
with a reason; both are logged and shown on the dashboard. Stops live on the exchange as bracket orders, so a
sleeping Mac cannot leave a position unprotected.
"""
import json
import logging
import math
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
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
Prefer letting winners run with a trailing stop over tiny targets. Never average down. Be concise and specific.
The state includes a brief from your news desk, which searches the web every 30 minutes. Weigh it: stand aside before
big scheduled events (CPI, FOMC), respect risk-off regimes, and use WebSearch only for a specific question the brief
does not answer."""

NEWS_SYSTEM = """You are the news desk for an autonomous crypto futures trader. Search the web now for what moved crypto
in the last 24 hours and what may move it in the next 24 hours. Run several searches covering:
1. crypto: ETF flows, SEC/CFTC actions and filings (ETF S-1 / 19b-4, lawsuits, approvals), exchange hacks or outages,
   stablecoin problems, token unlocks, large liquidations, whale and treasury-company moves
2. macro: US CPI / PCE / jobs data, Fed and FOMC speakers and rate expectations, dollar, bond yields, oil
3. politics and war: conflicts, sanctions, tariffs, elections, anything that can spike inflation or fear
4. stocks: Nasdaq / S&P moves, big tech earnings, overall risk appetite
5. India: RBI, crypto tax or regulation news, Delta Exchange notices
Prefer primary and reputable sources and give dates. Never invent news; if you cannot confirm something, leave it out.
Then judge the effect on crypto. Set wake_trader to true only if something NEW since the previous brief clearly changes
the risk of open positions or creates a strong opportunity now."""

WATCH_SYSTEM = """You are the risk watcher for open crypto futures positions. A held coin just moved sharply. Search the web
quickly for why (news, liquidations, hacks, macro data, a big market-wide move). Then decide per position: hold (no
action), tighten the stop (move_stop), or close. You cannot open trades. Normal noise is not a reason to act; act when
the cause threatens the trade's thesis, or to lock in a large open profit."""


def schema(symbols: List[str], kinds=("open", "close", "move_stop")) -> Dict[str, Any]:
    return {"type": "object", "required": ["market_view", "actions"], "properties": {
        "market_view": {"type": "string", "maxLength": 500},
        "actions": {"type": "array", "maxItems": 6, "items": {"type": "object", "required": ["type", "symbol", "reason"], "properties": {
            "type": {"type": "string", "enum": list(kinds)},
            "symbol": {"type": "string", "enum": symbols or ["NONE"]},
            "side": {"type": "string", "enum": ["long", "short"]},
            "risk_pct": {"type": "number", "minimum": 0, "maximum": 100, "description": "% of equity lost if the stop is hit"},
            "leverage": {"type": "number", "minimum": 1, "maximum": 200},
            "stop_price": {"type": "number"},
            "take_profit_price": {"type": ["number", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string", "maxLength": 400}}}}}}


NEWS_SCHEMA = {"type": "object", "required": ["regime", "summary", "events", "upcoming", "wake_trader"], "properties": {
    "regime": {"type": "string", "enum": ["risk_on", "neutral", "risk_off"]},
    "summary": {"type": "string", "maxLength": 700},
    "events": {"type": "array", "maxItems": 10, "items": {"type": "object", "required": ["headline", "impact", "bias"], "properties": {
        "headline": {"type": "string", "maxLength": 200}, "when": {"type": "string", "maxLength": 40},
        "impact": {"type": "string", "enum": ["high", "medium", "low"]},
        "bias": {"type": "string", "enum": ["bullish", "bearish", "mixed"]},
        "coins": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "source": {"type": "string", "maxLength": 80}}}},
    "upcoming": {"type": "array", "maxItems": 8, "items": {"type": "object", "required": ["event", "time_utc", "impact"], "properties": {
        "event": {"type": "string", "maxLength": 120}, "time_utc": {"type": "string", "maxLength": 40},
        "impact": {"type": "string", "enum": ["high", "medium", "low"]}}}},
    "wake_trader": {"type": "boolean"},
    "wake_reason": {"type": "string", "maxLength": 200}}}


class ClaudeAgent:
    def __init__(self, s: Settings, run: Callable = subprocess.run):
        self.s, self.run = s, run

    def ask(self, system: str, prompt: str, out_schema: Dict[str, Any], web: bool, timeout: int) -> Dict[str, Any]:
        """One clean headless Claude call; the structured answer plus `cost_usd`."""
        tools = ["WebSearch"] if web else []
        cmd = [self.s.claude_bin, "-p", prompt, "--model", self.s.agent_model, "--output-format", "json",
               "--json-schema", json.dumps(out_schema), "--system-prompt", system, "--no-session-persistence",
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", ""]
        cmd += ["--tools", *tools, "--allowedTools", *tools] if tools else ["--tools", ""]
        try:
            done = self.run(cmd, capture_output=True, text=True, timeout=timeout)
            out = json.loads(done.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            raise ExchangeError(f"Claude did not answer: {e}") from e
        if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
            raise ExchangeError(f"Claude error: {str(out.get('result') or out)[:300]}")
        return out["structured_output"] | {"cost_usd": float(out.get("total_cost_usd") or 0)}

    def _actions(self, system, context, symbols, kinds, web) -> Dict[str, Any]:
        prompt = ("Current state (JSON):\n" + json.dumps(context, indent=1, default=str) +
                  "\n\nDecide your actions now. An empty actions list is a valid answer. Reply with the JSON schema.")
        d = self.ask(system, prompt, schema(symbols, kinds), web, self.s.claude_timeout)
        return {"market_view": str(d.get("market_view", ""))[:500], "cost_usd": d["cost_usd"],
                "actions": [x for x in d.get("actions", []) if x.get("symbol") in symbols and x.get("type") in kinds][:6]}

    def decide(self, context: Dict[str, Any], symbols: List[str]) -> Dict[str, Any]:
        return self._actions(SYSTEM, context, symbols, ("open", "close", "move_stop"), self.s.claude_web)

    def watch(self, context: Dict[str, Any], symbols: List[str]) -> Dict[str, Any]:
        return self._actions(WATCH_SYSTEM, context, symbols, ("close", "move_stop"), True)

    def news(self, previous: Optional[Dict[str, Any]], now_utc: str) -> Dict[str, Any]:
        prev = {k: previous.get(k) for k in ("at_utc", "regime", "summary", "events")} if previous else None
        prompt = (f"Time now (UTC): {now_utc}\nYour previous brief: {json.dumps(prev, default=str)}\n\n"
                  "Research now and reply with the JSON schema.")
        return self.ask(NEWS_SYSTEM, prompt, NEWS_SCHEMA, True, max(self.s.claude_timeout, 420))


# --- the loop ------------------------------------------------------------------------------------------
class AgentLoop:
    def __init__(self, s: Settings, ledger, data_delta, trader: Optional[DeltaTrader] = None, claude=None, clock=time.time):
        if s.agent_mode == "live" and s.agent_live_confirm != LIVE_PHRASE:
            raise ExchangeError(f'live mode needs KTRADE_AGENT_LIVE_CONFIRM="{LIVE_PHRASE}" in .env')
        self.s, self.ledger, self.data, self.clock = s, ledger, data_delta, clock
        self.trader = trader or DeltaTrader(LIVE_URL if s.agent_mode == "live" else DEMO_URL, s.delta_key, s.delta_secret, clock=clock)
        self.claude = claude or ClaudeAgent(s)
        self.status: Dict[str, Any] = {"mode": s.agent_mode, "enabled": True, "news": self.brief(),
                                       "claude": json.loads(ledger.get("agent_claude") or "null")}
        self._snap_at = 0.0
        self._marks: Dict[str, List[tuple]] = {}            # recent (time, mark) per held coin, for shock detection
        self._watched: Dict[str, float] = {}                # last risk-watcher call per coin

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
        if not ctx["halted"] and self.watch(ctx):
            ctx = self.context()
        period = self.s.agent_cycle_minutes * 60
        slot = int(self.clock() // period)
        due = int(self.ledger.get("agent_last_slot", "-1")) != slot and self.clock() % period >= 150
        woken = self.ledger.get("agent_wake") and self.clock() - float(self.ledger.get("agent_last_cycle_at") or 0) >= 900
        if not ctx["halted"] and (due or woken):
            if due:
                self.ledger.put("agent_last_slot", str(slot))
            self.cycle(ctx, woken=self.ledger.get("agent_wake") or "")
            ctx = self.context()
        self.status.update(mode=self.s.agent_mode, live=self.trader.is_live, last_tick=self.clock(), equity=ctx["equity"],
                           available=ctx["available"], start_equity=ctx["start_equity"], day_start=ctx["day_start"],
                           entries_today=ctx["entries_today"], halted=ctx["halted"], next_cycle=(slot + 1) * period + 150,
                           positions=[p | ctx["stops"].get(p["symbol"], {}) for p in ctx["positions"]],
                           caps={"risk_pct": self.s.agent_max_risk_pct, "leverage": self.s.agent_max_leverage,
                                 "positions": self.s.agent_max_positions, "entries_per_day": self.s.agent_max_entries_per_day,
                                 "daily_loss_pct": self.s.agent_daily_loss_pct, "floor": self.s.agent_floor_equity,
                                 "shock_pct": self.s.agent_shock_pct})
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

    # news desk ------------------------------------------------------------------------------------------
    def brief(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self.ledger.get("agent_news") or "null")
        except ValueError:
            return None

    def news_tick(self, force: bool = False) -> Optional[Dict[str, Any]]:
        """Run the news desk when due (its own thread calls this; a web-research call takes a few minutes)."""
        prev = self.brief()
        if not force and prev and self.clock() - prev.get("at", 0) < self.s.agent_news_minutes * 60:
            return None
        now_utc = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d %H:%M")
        try:
            b = self.claude.news(prev, now_utc)
        except ExchangeError as e:
            log.error("news desk: %s", e)
            self.status["news_error"] = str(e)
            return None
        b |= {"at": self.clock(), "at_utc": now_utc}
        self.ledger.put("agent_news", json.dumps(b))
        self.ledger.decide(BOOK, "*", "news brief", 0, b.get("regime", "").replace("_", "-"), {"reason": b.get("summary", "")})
        self.status.update(news=b, news_error=None)
        log.info("news desk: %s | %s", b.get("regime"), str(b.get("summary", ""))[:160])
        if b.get("wake_trader"):
            self.ledger.put("agent_wake", b.get("wake_reason") or "breaking news")
            log.info("news desk woke the trader: %s", b.get("wake_reason"))
        return b

    def news_for_trader(self) -> Optional[Dict[str, Any]]:
        b = self.brief()
        if not b:
            return None
        age = (self.clock() - b.get("at", 0)) / 60
        return {"age_minutes": round(age)} | ({"stale": "older than 3 hours"} if age > 180 else {}) | \
            {k: b.get(k) for k in ("regime", "summary", "events", "upcoming")}

    # risk watcher -----------------------------------------------------------------------------------------
    def shocks(self, ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Held coins that moved at least agent_shock_pct within the last 15 minutes."""
        now, out = self.clock(), []
        held = {p["symbol"]: p for p in ctx["positions"]}
        self._marks = {k: v for k, v in self._marks.items() if k in held}
        for sym, p in held.items():
            mark = (ctx["quotes"].get(sym) or {}).get("mark") or p["mark"]
            hist = [(t, m) for t, m in self._marks.get(sym, []) if now - t <= 900] + [(now, mark)]
            self._marks[sym] = hist
            move = (mark / hist[0][1] - 1) * 100 if hist[0][1] else 0
            if abs(move) >= self.s.agent_shock_pct and now - self._watched.get(sym, 0) >= 900:
                out.append({"symbol": sym, "move_15m_pct": round(move, 2), "against_you": (move < 0) == (p["side"] == "long")})
        return out

    def watch(self, ctx: Dict[str, Any]) -> bool:
        hits = self.shocks(ctx)
        if not hits:
            return False
        for h in hits:
            self._watched[h["symbol"]] = self.clock()
        held = sorted({p["symbol"] for p in ctx["positions"]})
        state = {"time_utc": datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d %H:%M"), "sharp_moves": hits,
                 "open_positions": [{k: p.get(k) for k in ("symbol", "side", "size", "entry", "mark", "unrealized", "liquidation")}
                                    | ctx["stops"].get(p["symbol"], {}) for p in ctx["positions"]],
                 "news_desk": self.news_for_trader()}
        log.info("risk watcher woken: %s", hits)
        try:
            d = self.claude.watch(state, held)
        except ExchangeError as e:
            log.error("risk watcher: %s", e)
            self.status["watch"] = {"at": self.clock(), "moves": hits, "error": str(e)}
            return False
        self.status["watch"] = {"at": self.clock(), "moves": hits, "view": d["market_view"], "actions": d["actions"]}
        self.ledger.decide(BOOK, ",".join(h["symbol"] for h in hits), "watch", 0,
                           f"risk watcher: {len(d['actions'])} action(s)", {"moves": hits, "view": d["market_view"]})
        for a in d["actions"]:
            self.act(a, ctx)
            ctx.update({k: v for k, v in self.context().items() if k != "allowed"})
        return True

    # decisions --------------------------------------------------------------------------------------------
    def cycle(self, ctx: Dict[str, Any], woken: str = "") -> None:
        self.ledger.put("agent_last_cycle_at", str(self.clock()))
        self.ledger.put("agent_wake", "")
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
            "time_utc": datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d %H:%M"),
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
            "news_desk": self.news_for_trader() or "no brief yet",
        } | ({"woken_early_because": woken} if woken else {})
        symbols = sorted(ctx["allowed"] | {p["symbol"] for p in ctx["positions"]})
        try:
            d = self.claude.decide(state, symbols)
        except ExchangeError as e:
            log.error("agent: %s", e)
            self.ledger.decide(BOOK, "*", "error", 0, "Claude unavailable", str(e))
            self.status["claude"] = {"at": self.clock(), "error": str(e)}
            return
        self.status["claude"] = {"at": self.clock(), "market_view": d["market_view"], "actions": d["actions"],
                                 "cost_usd": d["cost_usd"], "woken": woken}
        self.ledger.put("agent_claude", json.dumps(self.status["claude"]))
        if not d["actions"]:
            self.ledger.decide(BOOK, "*", "hold", 0, "no trade", {"reason": d["market_view"]})
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
