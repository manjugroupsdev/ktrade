"""Delta Exchange India public market data. No API key: prices, contracts and candles are public."""
import time
from datetime import datetime
from typing import Any, Dict

import pandas as pd
import requests

RESOLUTION_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class DeltaError(RuntimeError):
    pass


class Delta:
    def __init__(self, base_url: str = "https://api.india.delta.exchange", session: Any = None,
                 retries: int = 3, clock=time.time, sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.http = session or requests.Session()
        self.retries, self.clock, self.sleep = retries, clock, sleep

    def _get(self, path: str, **params) -> Any:
        last = None
        for attempt in range(self.retries):
            try:
                res = self.http.get(self.base_url + path, params=params or None, timeout=15,
                                    headers={"Accept": "application/json", "User-Agent": "ktrade-paper"})
                body = res.json()
                if res.status_code == 200 and body.get("success"):
                    return body["result"]
                last = f"HTTP {res.status_code}: {str(body.get('error') or body)[:200]}"
                if res.status_code < 500 and res.status_code != 429:
                    break                                   # a client error will not fix itself
            except (requests.RequestException, ValueError) as e:
                last = str(e)
            self.sleep(2 ** attempt)
        raise DeltaError(f"{path}: {last}")

    def perpetuals(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for p in self._get("/v2/products", contract_types="perpetual_futures", page_size=500):
            if p.get("state") != "live" or p.get("trading_status", "operational") != "operational":
                continue
            launched = p.get("launch_time")
            out[p["symbol"]] = {
                "contract_value": float(p["contract_value"]),
                "tick": float(p.get("tick_size") or 0),
                "taker_fee": float(p.get("taker_commission_rate") or 0.0005),
                "launched": datetime.fromisoformat(launched.replace("Z", "+00:00")).timestamp() if launched else None,
            }
        return out

    def tickers(self) -> Dict[str, Dict[str, float]]:
        out = {}
        for t in self._get("/v2/tickers", contract_types="perpetual_futures"):
            q = t.get("quotes") or {}
            bid, ask = float(q.get("best_bid") or 0), float(q.get("best_ask") or 0)
            if not bid or not ask or ask < bid:
                continue                                    # no two-sided market: never invent a price
            out[t["symbol"]] = {
                "bid": bid, "ask": ask, "last": float(t.get("close") or (bid + ask) / 2),
                "turnover_usd": float(t.get("turnover_usd") or 0),
                "funding_rate": float(t.get("funding_rate") or 0),     # percent per 8 hours
                "ts": int(t.get("timestamp") or 0) / 1e6,
            }
        return out

    def candles(self, symbol: str, resolution: str = "1h", days: int = 40) -> pd.DataFrame:
        """Closed candles only, oldest first."""
        sec = RESOLUTION_SECONDS[resolution]
        end = int(self.clock())
        rows = self._get("/v2/history/candles", symbol=symbol, resolution=resolution, start=end - days * 86400, end=end)
        df = pd.DataFrame(rows or [], columns=["time", "open", "high", "low", "close", "volume"])
        if df.empty:
            return df
        df = df.astype({"time": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})
        df = df.drop_duplicates("time").sort_values("time")
        return df[df["time"] + sec <= end].reset_index(drop=True)
