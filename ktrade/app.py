"""Read-only dashboard API. Bound to localhost; requests arriving through a proxy or tunnel need the panel token."""
import hmac
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from ktrade.ledger import BOOKS

STATIC = os.path.join(os.path.dirname(__file__), "static")


def create_app(engine, ledger, s) -> FastAPI:
    app = FastAPI(title="ktrade", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def proxied_calls_need_token(request: Request, call_next):
        proxied = any(h in request.headers for h in ("x-forwarded-for", "cf-connecting-ip", "forwarded"))
        if proxied and request.url.path.startswith("/api/"):
            given = request.headers.get("x-panel-token", "")
            if not s.panel_token or not hmac.compare_digest(given.encode(), s.panel_token.encode()):
                return JSONResponse({"detail": "panel token required"}, status_code=401)
        return await call_next(request)

    @app.get("/api/status")
    def status():
        return engine.status | {"settings": {"risk_per_trade": s.risk_per_trade, "max_positions": s.max_positions,
                                             "daily_loss_limit": s.daily_loss_limit, "stop_atr": s.stop_atr,
                                             "take_profit_r": s.take_profit_r, "max_hold_hours": s.max_hold_hours,
                                             "bar_hours": s.bar_hours, "trail": s.trail, "trend_exit": s.trend_exit,
                                             "model": s.claude_model, "start_equity": s.start_equity}}

    @app.get("/api/research")
    def research():
        path = os.path.join(os.path.dirname(s.db_path) or ".", "research", "report.json")
        if not os.path.exists(path):
            return {"available": False, "hint": "run: python scripts/research.py"}
        with open(path) as f:
            return {"available": True} | json.load(f)

    @app.get("/api/curve")
    def curve(since: float = 0):
        return {b: ledger.curve(b, since) for b in BOOKS}

    @app.get("/api/trades")
    def trades(book: str = "", limit: int = 100):
        return ledger.trades(book or None, limit)

    @app.get("/api/decisions")
    def decisions(limit: int = 60):
        return ledger.decisions(limit)

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    return app
