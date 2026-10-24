from fastapi.testclient import TestClient

from ktrade.app import create_app
from ktrade.config import Settings
from ktrade.council import CouncilError
from ktrade.engine import Engine
from conftest import FakeCouncil


def engine_with(settings, delta, ledger, clock, council):
    return Engine(settings, delta, ledger, council, clock)


def test_hourly_cycle_both_books_trade_the_same_candidates(settings, delta, ledger, clock):
    council = FakeCouncil(entries=[{"symbol": "UPUSD", "action": "long", "conviction": 0.8, "thesis": "breakout"},
                                   {"symbol": "DNUSD", "action": "short", "conviction": 0.4, "thesis": "weak idea"}])
    e = engine_with(settings, delta, ledger, clock, council)
    st = e.tick()
    base = {(p["symbol"], p["side"]) for p in ledger.positions("baseline")}
    ai = {(p["symbol"], p["side"]) for p in ledger.positions("ai")}
    assert base == {("UPUSD", 1), ("DNUSD", -1)}                       # rules: strong trends both ways
    assert ai == {("UPUSD", 1)}                                        # Claude's 0.4 idea is below the 0.6 bar
    assert council.calls[0]["candidates"][:2] in (["UPUSD", "DNUSD"], ["DNUSD", "UPUSD"])
    assert st["books"]["ai"]["open"] == 1 and st["scan"]["candidates"]
    e.tick()                                                           # same hour: no second cycle
    assert len(council.calls) == 1
    clock.t += 3600
    e.tick()
    assert len(council.calls) == 2 and len(ledger.positions("ai")) == 1    # already holding: no duplicate


def test_stops_targets_and_time_limit_close_positions(settings, delta, ledger, clock):
    e = engine_with(settings, delta, ledger, clock, FakeCouncil())
    e.tick()
    up = next(p for p in ledger.positions("baseline") if p["symbol"] == "UPUSD")
    dn = next(p for p in ledger.positions("baseline") if p["symbol"] == "DNUSD")
    delta.set_price("UPUSD", up["stop"] * 0.999)                       # long stopped out
    delta.set_price("DNUSD", dn["target"] * 0.999)                     # short reaches target
    clock.t += 60
    e.tick()
    trades = {t["symbol"]: t for t in ledger.trades("baseline")}
    assert trades["UPUSD"]["exit_reason"] == "stop" and trades["UPUSD"]["net"] < 0
    assert trades["DNUSD"]["exit_reason"] == "target" and trades["DNUSD"]["net"] > 0
    loss_at_stop = -trades["UPUSD"]["net"]
    assert loss_at_stop <= settings.risk_per_trade * settings.start_equity * 1.2     # the stop held the risk budget (gap allowance)


def test_time_limit(settings, delta, ledger, clock):
    e = engine_with(settings, delta, ledger, clock, FakeCouncil())
    e.tick()
    clock.t += settings.max_hold_hours * 3600 + 1
    for s in ("UPUSD", "DNUSD"):
        delta.set_price(s, delta.quotes[s]["last"])
    e.manage_exits(delta.tickers())
    assert {t["exit_reason"] for t in ledger.trades("baseline")} == {"time limit"}


def test_claude_can_exit_and_council_failure_only_stops_ai_entries(settings, delta, ledger, clock):
    council = FakeCouncil(entries=[{"symbol": "UPUSD", "action": "long", "conviction": 0.9, "thesis": "x"}])
    e = engine_with(settings, delta, ledger, clock, council)
    e.tick()
    assert len(ledger.positions("ai")) == 1
    council.entries, council.exits = [], [{"symbol": "UPUSD", "action": "exit", "reason": "news turned"}]
    clock.t += 3600
    e.tick()
    assert ledger.positions("ai") == [] and ledger.trades("ai")[0]["exit_reason"].startswith("Claude exit")
    council.error = CouncilError("rate limited")
    clock.t += 3600
    st = e.tick()
    assert ledger.positions("ai") == [] and "rate limited" in st["council"]["error"]
    assert ledger.positions("baseline")                                # rules book unaffected


def test_daily_loss_limit_blocks_new_entries(settings, delta, ledger, clock):
    e = engine_with(settings, delta, ledger, clock, FakeCouncil())
    ledger.day_start_equity("baseline", "2027-01-15", 1000.0)
    ledger.put("x", "y")
    with ledger._db() as db:
        db.execute("UPDATE books SET cash = 960 WHERE name='baseline'")
        db.execute("DELETE FROM day_start")
        db.execute("INSERT INTO day_start VALUES ('baseline', ?, 1000)",
                   (__import__("datetime").datetime.fromtimestamp(clock(), __import__("ktrade.engine", fromlist=["IST"]).IST).strftime("%Y-%m-%d"),))
    e.tick()
    assert ledger.positions("baseline") == []
    assert any("daily loss" in d["outcome"] for d in ledger.decisions())


def test_dashboard_api_and_token_gate(settings, delta, ledger, clock):
    s = Settings(db_path=settings.db_path, panel_token="s3cret")
    e = engine_with(s, delta, ledger, clock, FakeCouncil())
    e.tick()
    c = TestClient(create_app(e, ledger, s))
    assert c.get("/api/status").json()["books"]["baseline"]["open"] == 2
    assert c.get("/api/status", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 401
    assert c.get("/api/status", headers={"x-forwarded-for": "1.2.3.4", "x-panel-token": "s3cret"}).status_code == 200
    assert "Claude vs Rules" in c.get("/").text
    assert set(c.get("/api/curve").json()) == {"ai", "baseline"}


def test_no_second_position_in_a_twin_of_an_open_one(settings, delta, ledger, clock):
    e = engine_with(settings, delta, ledger, clock, FakeCouncil())
    e.tick()
    held = {p["symbol"] for p in ledger.positions("baseline")}
    assert "UPUSD" in held
    delta.products["UP2USD"] = dict(delta.products["UPUSD"])
    delta.frames["UP2USD"] = delta.frames["UPUSD"].copy()
    delta.quotes["UP2USD"] = dict(delta.quotes["UPUSD"])
    clock.t += 3600
    e.tick()
    assert "UP2USD" not in {p["symbol"] for p in ledger.positions("baseline")}
