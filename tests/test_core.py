import json
import subprocess
from types import SimpleNamespace

import pytest

from ktrade import risk
from ktrade.config import Settings
from ktrade.council import ClaudeCouncil, CouncilError
from ktrade.scanner import scan
from ktrade.signals import features, trend_score
from conftest import NOW, make_candles


# --- risk ------------------------------------------------------------------------------------------
def test_size_fits_the_risk_budget_including_costs():
    s = Settings(take_profit_r=2.0)
    p = risk.plan_entry(s, "BTCUSD", 1, 80_000.0, 400.0, 0.001, 1000.0, 0.0)
    per_contract = (3 * 400 + (2 * s.taker_fee + 2 * s.slippage_bps / 1e4) * 80_000) * 0.001
    assert p.qty == int(5.0 // per_contract) and p.risk_usd <= 5.0
    assert p.stop == 80_000 - 1200 and p.target == 80_000 + 2400


def test_short_levels_are_mirrored_and_too_risky_is_refused():
    s = Settings(take_profit_r=2.0)
    p = risk.plan_entry(s, "X", -1, 100.0, 1.0, 1.0, 1000.0, 0.0)
    assert p.stop == 103.0 and p.target == 94.0
    assert "budget" in risk.plan_entry(s, "X", 1, 100.0, 10.0, 1.0, 1000.0, 0.0)       # $30 stop on one contract > $5
    assert risk.plan_entry(Settings(), "X", 1, 100.0, 1.0, 1.0, 1000.0, 0.0).target is None   # default: no fixed target


def test_exposure_cap_limits_quantity():
    s = Settings(max_gross_leverage=1.0)
    assert "exposure cap" in risk.plan_entry(s, "X", 1, 100.0, 0.01, 1.0, 1000.0, 1000.0)
    assert risk.plan_entry(s, "X", 1, 100.0, 0.01, 1.0, 1000.0, 500.0).qty == 5


# --- ledger ----------------------------------------------------------------------------------------
def test_round_trip_charges_fees_and_funding(ledger, clock):
    plan = SimpleNamespace(symbol="UPUSD", side=1, qty=10, stop=90.0, target=120.0, contract_value=1.0)
    ledger.open("ai", plan, 100.0, 0.001, 0.01, "test")
    assert ledger.cash("ai") == pytest.approx(1000 - 1.0)                              # entry fee 10 x 100 x 0.1%
    marks = {"UPUSD": {"bid": 110.0, "ask": 110.1}}
    assert ledger.equity("ai", marks) == pytest.approx(999 + 100)
    clock.t += 16 * 3600                                                                 # two funding periods
    (pos,) = ledger.positions("ai")
    r = ledger.close(pos, 110.0, 0.001, "target")
    funding = 1000 * 0.01 / 100 * 2
    assert r["net"] == pytest.approx(100 - 1.0 - 1.1 - funding, abs=0.01)
    assert ledger.cash("ai") == pytest.approx(1000 + r["net"], abs=0.01)
    assert ledger.summary("ai", marks)["trades"] == 1 and ledger.positions() == []


def test_short_receives_positive_funding(ledger, clock):
    plan = SimpleNamespace(symbol="DNUSD", side=-1, qty=10, stop=110.0, target=80.0, contract_value=1.0)
    ledger.open("baseline", plan, 100.0, 0.0, 0.01, "test")
    clock.t += 8 * 3600
    r = ledger.close(ledger.positions("baseline")[0], 100.0, 0.0, "time limit")
    assert r["net"] == pytest.approx(1000 * 0.01 / 100)


# --- signals & scanner -----------------------------------------------------------------------------
def test_trend_score_reads_direction():
    assert trend_score(make_candles(100, 0.4)) == 1.0
    assert trend_score(make_candles(100, -0.4)) == -1.0
    f = features(make_candles(100, 0.4))
    assert f["vol_ratio"] > 2 and f["ret_7d_pct"] > 0


def test_scanner_skips_fresh_thin_and_excluded_listings(delta, settings):
    r = scan(delta, settings, NOW)
    assert set(r["universe"]) == {"UPUSD", "DNUSD", "FLATUSD"}
    assert [c["symbol"] for c in r["candidates"]][:2] in (["UPUSD", "DNUSD"], ["DNUSD", "UPUSD"])
    assert r["candidates"][-1]["symbol"] == "FLATUSD"
    r2 = scan(delta, Settings(db_path=settings.db_path, exclude=("UPUSD",)), NOW)
    assert "UPUSD" not in r2["universe"]


# --- council ---------------------------------------------------------------------------------------
def fake_run(stdout="", code=0, exc=None):
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")
    run.calls = calls
    return run


CANDS = [{"symbol": "UPUSD", "trend": 1.0, "price": 1.0, "contract_value": 0.01}]


def test_council_uses_a_clean_session_and_parses_structured_output():
    out = {"structured_output": {"market_view": "risk-on", "entries": [
        {"symbol": "UPUSD", "action": "long", "conviction": 1.7, "thesis": "breakout"},
        {"symbol": "HACKUSD", "action": "long", "conviction": 0.9, "thesis": "not shown"}], "exits": []},
        "total_cost_usd": 0.03, "is_error": False}
    run = fake_run(json.dumps(out))
    d = ClaudeCouncil(Settings(), run).decide(CANDS, [], 1000.0)
    assert d["entries"] == [{"symbol": "UPUSD", "action": "long", "conviction": 1.0, "thesis": "breakout", "invalidation": ""}]
    cmd = run.calls[0]
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd and "WebSearch" in cmd
    sch = json.loads(cmd[cmd.index("--json-schema") + 1])
    assert sch["properties"]["entries"]["items"]["properties"]["symbol"]["enum"] == ["UPUSD"]


@pytest.mark.parametrize("run", [fake_run("not json", 1), fake_run(json.dumps({"is_error": True, "result": "rate limited"})),
                                 fake_run(exc=subprocess.TimeoutExpired("claude", 1)), fake_run(exc=FileNotFoundError("claude"))])
def test_council_failures_raise_council_error(run):
    with pytest.raises(CouncilError):
        ClaudeCouncil(Settings(), run).decide(CANDS, [], 1000.0)


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KTRADE_RISK_PER_TRADE", "0.002")
    monkeypatch.setenv("KTRADE_CLAUDE_WEB", "false")
    monkeypatch.setenv("KTRADE_EXCLUDE", "irysusd, akeusd")
    s = Settings.from_env(str(tmp_path / "missing.env"))
    assert s.risk_per_trade == 0.002 and s.claude_web is False and s.exclude == ("IRYSUSD", "AKEUSD")


def test_parabolic_pumps_are_skipped_not_ranked_first(delta, settings):
    delta.frames["UPUSD"] = make_candles(1000, 0.4)
    pumped = delta.frames["UPUSD"].copy()
    pumped.loc[pumped.index[-24:], ["open", "high", "low", "close"]] *= 1.6         # +60% in a day
    delta.frames["UPUSD"] = pumped
    r = scan(delta, settings, NOW)
    assert "UPUSD" not in [c["symbol"] for c in r["candidates"]]
    assert r["too_wild"][0]["symbol"] == "UPUSD"


def test_twin_coins_count_as_one_bet(delta, settings):
    delta.products["UP2USD"] = dict(delta.products["UPUSD"])
    delta.frames["UP2USD"] = delta.frames["UPUSD"].copy()
    delta.quotes["UP2USD"] = dict(delta.quotes["UPUSD"])
    r = scan(delta, settings, NOW)
    syms = [c["symbol"] for c in r["candidates"]]
    assert ("UPUSD" in syms) != ("UP2USD" in syms)
    assert r["duplicates"][0]["same_bet_as"] in ("UPUSD", "UP2USD")
