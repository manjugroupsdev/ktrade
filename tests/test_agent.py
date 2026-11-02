from dataclasses import replace

import pytest

from ktrade.agent import LIVE_PHRASE, AgentLoop, check_move_stop, check_open
from ktrade.config import Settings
from ktrade.exchange import ExchangeError
from conftest import NOW

CAPS = Settings(agent_max_risk_pct=5, agent_max_leverage=10, agent_max_positions=3, agent_max_entries_per_day=6,
                agent_daily_loss_pct=15, agent_floor_equity=50, agent_min_stop_pct=0.4, agent_max_stop_pct=8)
PRODUCTS = {"BTCUSD": {"id": 1, "contract_value": 0.001, "tick": 0.5}, "ETHUSD": {"id": 2, "contract_value": 0.01, "tick": 0.05}}
QUOTES = {"BTCUSD": {"bid": 80000.0, "ask": 80001.0, "mark": 80000.5}, "ETHUSD": {"bid": 2300.0, "ask": 2300.1, "mark": 2300.05}}


def ctx(**kw):
    base = {"equity": 100.0, "available": 100.0, "day_start": 100.0, "entries_today": 0, "positions": [], "stops": {},
            "quotes": QUOTES, "products": PRODUCTS, "halted": None, "allowed": {"BTCUSD", "ETHUSD"}}
    return base | kw


def open_btc(**kw):
    return {"type": "open", "symbol": "BTCUSD", "side": "long", "risk_pct": 5, "leverage": 10, "stop_price": 78800.0} | kw


# --- guard ------------------------------------------------------------------------------------------------
def test_size_respects_the_5_percent_risk_cap_even_if_claude_asks_for_more():
    p = check_open(CAPS, open_btc(risk_pct=50), ctx())
    assert p.size >= 1 and p.risk_usd <= 5.0 + 1e-9                     # never more than 5% of $100
    assert p.leverage <= 10 and p.notional <= 10 * 100


def test_leverage_is_capped_and_kept_below_liquidation_distance():
    p = check_open(CAPS, open_btc(leverage=100, stop_price=80001 * (1 - 0.05)), ctx())
    assert p.leverage == 10                                              # 100x asked, 10x cap; 5% stop fits under 80/10=8%
    p = check_open(CAPS, open_btc(leverage=10, stop_price=80001 * (1 - 0.079)), ctx())
    assert isinstance(p, str) or p.leverage <= 10                        # 7.9% stop: at most 10x (80/7.9)


@pytest.mark.parametrize("action, reason", [
    (open_btc(stop_price=81000.0), "wrong side"),
    (open_btc(stop_price=79900.0), "allowed"),                          # 0.13% stop: too tight
    (open_btc(stop_price=70000.0), "allowed"),                          # 12.5% stop: too wide
    (open_btc(symbol="DOGEUSD"), "tradable"),
    (open_btc(side="sideways"), "side"),
    (open_btc(take_profit_price=79000.0), "take-profit"),
])
def test_bad_orders_are_refused(action, reason):
    assert reason in check_open(CAPS, action, ctx())


def test_limits_on_positions_entries_daily_loss_and_halt():
    held = [{"symbol": s, "side": "long", "size": 1, "entry": 1, "mark": 1} for s in ("A", "B", "C")]
    assert "max 3" in check_open(CAPS, open_btc(), ctx(positions=held))
    assert "entries today" in check_open(CAPS, open_btc(), ctx(entries_today=6))
    assert "daily loss" in check_open(CAPS, open_btc(), ctx(equity=84.0, day_start=100.0))
    assert "halted" in check_open(CAPS, open_btc(), ctx(halted="floor"))
    assert "already holding" in check_open(CAPS, open_btc(), ctx(positions=[{"symbol": "BTCUSD", "side": "long"}]))


def test_stops_only_tighten():
    pos = [{"symbol": "BTCUSD", "side": "long", "size": 1, "entry": 79000, "mark": 80000}]
    c = ctx(positions=pos, stops={"BTCUSD": {"stop": 78000.0}})
    assert check_move_stop({"symbol": "BTCUSD", "stop_price": 79500.0}, c) == 79500.0
    assert "only tighten" in check_move_stop({"symbol": "BTCUSD", "stop_price": 77000.0}, c)
    assert "past the current price" in check_move_stop({"symbol": "BTCUSD", "stop_price": 80500.0}, c)


# --- loop with a fake exchange and a fake Claude --------------------------------------------------------
class FakeTrader:
    is_live = False

    def __init__(self, equity=100.0, fail_protect=False):
        self.eq, self.pos, self.stops, self.calls, self.fail_protect = equity, [], {}, [], fail_protect

    def equity(self): return {"equity": self.eq, "available": self.eq}
    def quotes(self): return QUOTES
    def products(self): return PRODUCTS
    def positions(self): return list(self.pos)
    def stop_orders(self): return dict(self.stops)
    def set_leverage(self, sym, lev): self.calls.append(("leverage", sym, lev))

    def market(self, sym, side, size, reduce_only=False):
        self.calls.append(("market", sym, side, size, reduce_only))
        if not reduce_only:
            q = QUOTES[sym]
            self.pos.append({"symbol": sym, "side": "long" if side == "buy" else "short", "size": size,
                             "entry": q["ask"] if side == "buy" else q["bid"], "mark": q["bid"], "unrealized": 0.0, "liquidation": 0})
        return {"id": 1}

    def protect(self, sym, stop, target):
        self.calls.append(("protect", sym, stop, target))
        if self.fail_protect:
            raise ExchangeError("stop rejected")
        self.stops[sym] = {"stop": stop} | ({"target": target} if target else {})

    def cancel_all(self, sym):
        self.calls.append(("cancel", sym)); self.stops.pop(sym, None)

    def close(self, sym):
        self.calls.append(("close", sym)); self.pos = [p for p in self.pos if p["symbol"] != sym]; self.stops.pop(sym, None)


class FakeClaude:
    def __init__(self, actions=None, error=None):
        self.actions, self.error, self.seen = actions or [], error, []

    def decide(self, state, symbols):
        self.seen.append((state, symbols))
        if self.error:
            raise ExchangeError(self.error)
        return {"market_view": "test", "actions": list(self.actions), "cost_usd": 0.02}

    def watch(self, state, symbols):
        self.seen.append(("watch", state, symbols))
        return {"market_view": "hack news", "actions": list(self.actions), "cost_usd": 0.03}

    def news(self, previous, now_utc):
        self.seen.append(("news", previous))
        return {"regime": "risk_off", "summary": "CPI hot", "events": [], "upcoming": [], "wake_trader": True,
                "wake_reason": "CPI beat", "cost_usd": 0.1}


class RealPrices:
    def tickers(self):
        return {s: {"bid": q["bid"], "ask": q["ask"], "turnover_usd": 1e9, "funding_rate": 0.0} for s, q in QUOTES.items()}

    def perpetuals(self): return {}


def loop(ledger, clock, trader, claude, **kw):
    return AgentLoop(replace(CAPS, agent_enabled=True, **kw), ledger, RealPrices(), trader, claude, clock)


def test_claude_opens_a_trade_and_the_stop_goes_to_the_exchange(ledger, clock):
    t = FakeTrader()
    a = loop(ledger, clock, t, FakeClaude([open_btc(take_profit_price=84000.0)]))
    a.cycle(a.context())
    assert [c[0] for c in t.calls] == ["leverage", "market", "protect"]
    assert t.stops["BTCUSD"] == {"stop": 78800.0, "target": 84000.0}
    assert any(d["outcome"].startswith("opened") for d in ledger.decisions())


def test_a_rejected_stop_closes_the_position_immediately(ledger, clock):
    t = FakeTrader(fail_protect=True)
    a = loop(ledger, clock, t, FakeClaude([open_btc()]))
    a.cycle(a.context())
    assert t.pos == [] and ("close", "BTCUSD") in t.calls
    assert any("refused the stop" in d["outcome"] for d in ledger.decisions())


def test_refusals_are_logged_and_nothing_is_sent(ledger, clock):
    t = FakeTrader()
    a = loop(ledger, clock, t, FakeClaude([open_btc(stop_price=81000.0)]))
    a.cycle(a.context())
    assert t.calls == [] and any(d["outcome"].startswith("refused") for d in ledger.decisions())


def test_floor_closes_everything_and_halts(ledger, clock):
    t = FakeTrader()
    t.pos = [{"symbol": "ETHUSD", "side": "short", "size": 1, "entry": 2300, "mark": 2400, "unrealized": -1, "liquidation": 0}]
    t.eq = 49.0
    claude = FakeClaude([open_btc()])
    a = loop(ledger, clock, t, claude)
    clock.t = NOW + 3600                                                 # a fresh hourly slot
    st = a.tick()
    assert t.pos == [] and "floor" in st["halted"] and claude.seen == []
    assert "halted" in check_open(a.s, open_btc(), a.context())


def test_claude_failure_means_no_trades(ledger, clock):
    t = FakeTrader()
    a = loop(ledger, clock, t, FakeClaude(error="rate limited"))
    a.cycle(a.context())
    assert t.calls == [] and "rate limited" in a.status["claude"]["error"]


def test_live_mode_needs_the_written_confirmation(ledger, clock):
    with pytest.raises(ExchangeError, match="LIVE_CONFIRM"):
        loop(ledger, clock, FakeTrader(), FakeClaude(), agent_mode="live")
    assert loop(ledger, clock, FakeTrader(), FakeClaude(), agent_mode="live", agent_live_confirm=LIVE_PHRASE)


def test_news_desk_brief_reaches_the_trader_and_wakes_it_early(ledger, clock):
    claude = FakeClaude()
    a = loop(ledger, clock, FakeTrader(), claude)
    a.cycle(a.context())                                                 # a normal hourly cycle just ran
    ledger.put("agent_last_slot", str(int(clock.t // 3600)))
    assert a.news_tick()["regime"] == "risk_off"
    assert a.news_tick() is None                                         # not due again for 30 minutes
    clock.t += 300
    a.tick()
    assert len([x for x in claude.seen if x[0] != "news"]) == 1          # woken, but not within 15 min of the last cycle
    clock.t += 900
    a.tick()
    state = claude.seen[-1][0]
    assert state["news_desk"]["summary"] == "CPI hot" and state["woken_early_because"] == "CPI beat"
    assert not ledger.get("agent_wake")


def test_risk_watcher_wakes_on_a_sharp_move_and_can_only_close_or_tighten(ledger, clock):
    t = FakeTrader()
    t.pos = [{"symbol": "BTCUSD", "side": "long", "size": 1, "entry": 80000, "mark": 80000, "unrealized": 0, "liquidation": 0}]
    claude = FakeClaude([{"type": "close", "symbol": "BTCUSD", "reason": "exchange hack"}])
    a = loop(ledger, clock, t, claude, agent_shock_pct=1.5)
    c = a.context()
    assert not a.watch(c)                                                # first price seen, no move yet
    clock.t += 300
    c["quotes"] = QUOTES | {"BTCUSD": {"bid": 78700.0, "ask": 78701.0, "mark": 78700.0}}   # -1.6% in 5 min
    assert a.watch(c)
    assert claude.seen[-1][0] == "watch" and claude.seen[-1][1]["sharp_moves"][0]["against_you"]
    assert ("close", "BTCUSD") in t.calls
    assert not a.shocks(c)                                               # quiet for 15 min after a call


def test_watcher_schema_forbids_opening():
    from ktrade.agent import schema
    assert schema(["BTCUSD"], ("close", "move_stop"))["properties"]["actions"]["items"]["properties"]["type"]["enum"] == ["close", "move_stop"]
