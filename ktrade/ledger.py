"""SQLite paper ledger for the two books ("ai" and "baseline"). Fills, fees, funding and decisions are all recorded."""
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

BOOKS = ("ai", "baseline")
SCHEMA = """
CREATE TABLE IF NOT EXISTS books (name TEXT PRIMARY KEY, start_equity REAL, cash REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS positions (id INTEGER PRIMARY KEY, book TEXT, symbol TEXT, side INTEGER, qty INTEGER,
    contract_value REAL, entry REAL, stop REAL, target REAL, opened_at REAL, funding_rate REAL, reason TEXT, decision_id INTEGER);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, book TEXT, symbol TEXT, side INTEGER, qty INTEGER, entry REAL,
    exit REAL, opened_at REAL, closed_at REAL, gross REAL, fees REAL, funding REAL, net REAL, exit_reason TEXT, entry_reason TEXT);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, at REAL, book TEXT, symbol TEXT, action TEXT,
    conviction REAL, outcome TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS snapshots (at REAL, book TEXT, equity REAL);
CREATE TABLE IF NOT EXISTS day_start (book TEXT, day TEXT, equity REAL, PRIMARY KEY (book, day));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Ledger:
    def __init__(self, path: str, start_equity: float = 1000.0, clock=time.time):
        self.path, self.clock = path, clock
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)
            for table in ("positions", "trades"):                 # databases created by v0.1 lack the risk column
                try:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN risk REAL")
                except sqlite3.OperationalError:
                    pass
            for b in BOOKS:
                db.execute("INSERT OR IGNORE INTO books VALUES (?, ?, ?, ?)", (b, start_equity, start_equity, clock()))

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # --- meta ----------------------------------------------------------------------------------
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def put(self, key: str, value: str) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    # --- positions -----------------------------------------------------------------------------
    def positions(self, book: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._db() as db:
            q = "SELECT * FROM positions" + (" WHERE book=?" if book else "") + " ORDER BY id"
            return [dict(r) for r in db.execute(q, (book,) if book else ())]

    def open(self, book: str, plan, fill: float, fee_rate: float, funding_rate: float, reason: str,
             decision_id: Optional[int] = None) -> int:
        fee = abs(plan.qty * plan.contract_value * fill) * fee_rate
        with self._db() as db:
            cur = db.execute("INSERT INTO positions (book, symbol, side, qty, contract_value, entry, stop, target, opened_at,"
                             " funding_rate, reason, decision_id, risk) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (book, plan.symbol, plan.side, plan.qty, plan.contract_value, fill, plan.stop, plan.target,
                              self.clock(), funding_rate, reason, decision_id, getattr(plan, "risk_usd", None)))
            db.execute("UPDATE books SET cash = cash - ? WHERE name=?", (fee, book))
            return cur.lastrowid

    def close(self, pos: Dict[str, Any], fill: float, fee_rate: float, reason: str) -> Dict[str, Any]:
        units = pos["qty"] * pos["contract_value"]
        gross = (fill - pos["entry"]) * pos["side"] * units
        fees = (abs(units * pos["entry"]) + abs(units * fill)) * fee_rate
        hours = (self.clock() - pos["opened_at"]) / 3600
        # Perpetual funding (percent per 8h): longs pay a positive rate, shorts receive it.
        funding = abs(units * pos["entry"]) * pos["funding_rate"] / 100 * hours / 8 * pos["side"]
        net = gross - fees - funding
        with self._db() as db:
            db.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
            # The entry fee was already taken from cash when the position opened.
            db.execute("UPDATE books SET cash = cash + ? WHERE name=?", (gross - abs(units * fill) * fee_rate - funding, pos["book"]))
            db.execute("INSERT INTO trades (book, symbol, side, qty, entry, exit, opened_at, closed_at, gross, fees, funding, net,"
                       " exit_reason, entry_reason, risk) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pos["book"], pos["symbol"], pos["side"], pos["qty"], pos["entry"], fill, pos["opened_at"], self.clock(),
                        round(gross, 4), round(fees, 4), round(funding, 4), round(net, 4), reason, pos["reason"], pos.get("risk")))
        return {"symbol": pos["symbol"], "net": round(net, 2), "reason": reason}

    def update_stop(self, position_id: int, stop: float) -> None:
        with self._db() as db:
            db.execute("UPDATE positions SET stop=? WHERE id=?", (stop, position_id))

    # --- accounting ----------------------------------------------------------------------------
    def cash(self, book: str) -> float:
        with self._db() as db:
            return float(db.execute("SELECT cash FROM books WHERE name=?", (book,)).fetchone()["cash"])

    def start_equity(self, book: str) -> float:
        with self._db() as db:
            return float(db.execute("SELECT start_equity FROM books WHERE name=?", (book,)).fetchone()["start_equity"])

    @staticmethod
    def unrealized(pos: Dict[str, Any], marks: Dict[str, Dict[str, float]]) -> float:
        t = marks.get(pos["symbol"])
        if not t:
            return 0.0
        exit_px = t["bid"] if pos["side"] > 0 else t["ask"]
        return (exit_px - pos["entry"]) * pos["side"] * pos["qty"] * pos["contract_value"]

    def equity(self, book: str, marks: Dict[str, Dict[str, float]]) -> float:
        return self.cash(book) + sum(self.unrealized(p, marks) for p in self.positions(book))

    def day_start_equity(self, book: str, day: str, equity_now: float) -> float:
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO day_start VALUES (?, ?, ?)", (book, day, equity_now))
            return float(db.execute("SELECT equity FROM day_start WHERE book=? AND day=?", (book, day)).fetchone()["equity"])

    def snapshot(self, book: str, equity: float) -> None:
        with self._db() as db:
            db.execute("INSERT INTO snapshots VALUES (?, ?, ?)", (self.clock(), book, round(equity, 4)))

    def decide(self, book: str, symbol: str, action: str, conviction: float, outcome: str, detail: Any) -> int:
        with self._db() as db:
            return db.execute("INSERT INTO decisions (at, book, symbol, action, conviction, outcome, detail) VALUES (?,?,?,?,?,?,?)",
                              (self.clock(), book, symbol, action, conviction, outcome,
                               detail if isinstance(detail, str) else json.dumps(detail))).lastrowid

    # --- reporting -----------------------------------------------------------------------------
    def trades(self, book: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        with self._db() as db:
            q = "SELECT * FROM trades" + (" WHERE book=?" if book else "") + " ORDER BY closed_at DESC LIMIT ?"
            return [dict(r) for r in db.execute(q, ((book, limit) if book else (limit,)))]

    def decisions(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))]

    def curve(self, book: str, since: float = 0) -> List[List[float]]:
        with self._db() as db:
            return [[r["at"], r["equity"]] for r in db.execute(
                "SELECT at, equity FROM snapshots WHERE book=? AND at>=? ORDER BY at", (book, since))]

    def summary(self, book: str, marks: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
        t = self.trades(book, limit=100000)
        wins, losses = [x["net"] for x in t if x["net"] > 0], [x["net"] for x in t if x["net"] <= 0]
        eq, start = self.equity(book, marks), self.start_equity(book)
        r_mult = [x["net"] / x["risk"] for x in t if x.get("risk")]
        curve = [e for _, e in self.curve(book)] + [eq]
        peak, max_dd = curve[0], 0.0
        for e in curve:
            peak = max(peak, e)
            max_dd = min(max_dd, (e - peak) / peak)
        return {"equity": round(eq, 2), "start": start, "return_pct": round((eq / start - 1) * 100, 2),
                "net_closed": round(sum(x["net"] for x in t), 2), "max_drawdown_pct": round(max_dd * 100, 2),
                "avg_r": round(sum(r_mult) / len(r_mult), 2) if r_mult else None,
                "open_risk": round(sum(p.get("risk") or 0 for p in self.positions(book)), 2),
                "trades": len(t), "wins": len(wins), "losses": len(losses),
                "win_rate": round(len(wins) / len(t) * 100, 1) if t else None,
                "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
                "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
                "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
                "fees": round(sum(x["fees"] for x in t), 2), "funding": round(sum(x["funding"] for x in t), 2),
                "open": len(self.positions(book))}
