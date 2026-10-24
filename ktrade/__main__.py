"""
    python -m ktrade run     # trading loop + dashboard on http://127.0.0.1:8060
    python -m ktrade once    # one full scan + decision cycle now, then print the books
    python -m ktrade scan    # just show what the scanner would hand to Claude
"""
import argparse
import json
import logging
import sys
import threading
import time

from ktrade.config import Settings
from ktrade.council import ClaudeCouncil
from ktrade.delta import Delta, DeltaError
from ktrade.engine import Engine
from ktrade.ledger import Ledger
from ktrade.scanner import scan

log = logging.getLogger("ktrade")
TICK_SECONDS = 30


def build(s: Settings) -> Engine:
    return Engine(s, Delta(s.delta_base_url), Ledger(s.db_path, s.start_equity), ClaudeCouncil(s))


def run(s: Settings, engine: Engine) -> None:
    import uvicorn
    from ktrade.app import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(engine, engine.ledger, s), host=s.host, port=s.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    log.info("dashboard on http://%s:%d | paper equity per book $%.0f | Claude model %s", s.host, s.port, s.start_equity, s.claude_model)
    failures = 0
    while True:
        try:
            engine.tick()
            failures, engine.status["last_error"] = 0, None
        except DeltaError as e:                   # network or exchange hiccup: keep going, positions are re-checked next tick
            failures += 1
            engine.status["last_error"] = str(e)
            if failures <= 3 or failures % 20 == 0:
                log.warning("market data unavailable (%d in a row): %s", failures, e)
        except Exception:                         # never let one bad tick stop the loop
            log.exception("tick failed")
        time.sleep(TICK_SECONDS)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ktrade", description="Claude-vs-rules crypto paper trading on Delta Exchange India")
    p.add_argument("command", choices=["run", "once", "scan"])
    p.add_argument("--env", default=".env")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    s = Settings.from_env(a.env)
    if a.command == "scan":
        r = scan(Delta(s.delta_base_url), s, time.time())
        print(f"{len(r['universe'])} liquid coins: {', '.join(r['universe'])}")
        if r["too_wild"]:
            print("skipped as too wild: " + ", ".join(f"{w['symbol']} (ATR {w['atr_pct']}%, 24h {w['ret_24h_pct']:+}%)" for w in r["too_wild"]))
        for c in r["candidates"]:
            print(f"  {c['symbol']:<10} score {c['score']:.2f}  trend {c['trend']:+.2f}  vol x{c['vol_ratio']:.2f}  "
                  f"ATR {c['atr_pct']:.2f}%  24h {c['ret_24h_pct']:+.1f}%  7d {c['ret_7d_pct']:+.1f}%")
        return 0
    engine = build(s)
    if a.command == "once":
        st = engine.tick(force_cycle=True)
        print(json.dumps({"books": st["books"], "council": st.get("council"), "positions": st.get("positions")}, indent=1, default=str))
        return 0
    run(s, engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
