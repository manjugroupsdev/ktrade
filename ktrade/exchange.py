"""
Authenticated Delta Exchange India client for the Claude agent book.

Demo (testnet) and live share this code; only the base URL and keys differ. Every position the agent opens gets
a stop-loss (and optional take-profit) held by the exchange itself as a bracket order, so a sleeping or offline
Mac cannot leave a position unprotected.
"""
import hashlib
import hmac
import json
import math
import time
from typing import Any, Dict, List, Optional

import requests

DEMO_URL = "https://cdn-ind.testnet.deltaex.org"
LIVE_URL = "https://api.india.delta.exchange"


class ExchangeError(RuntimeError):
    pass


class DeltaTrader:
    def __init__(self, base_url: str, key: str, secret: str, session: Any = None, clock=time.time):
        if not key or not secret:
            raise ExchangeError("Delta API key and secret are not set (KTRADE_DELTA_KEY / KTRADE_DELTA_SECRET in .env)")
        self.base_url, self.key, self.secret = base_url.rstrip("/"), key, secret
        self.http, self.clock = session or requests.Session(), clock
        self._products: Dict[str, Dict[str, Any]] = {}

    @property
    def is_live(self) -> bool:
        return self.base_url == LIVE_URL

    # --- transport -------------------------------------------------------------------------------
    def _request(self, method: str, path: str, params: Optional[Dict] = None, body: Optional[Dict] = None,
                 auth: bool = True) -> Any:
        query = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"User-Agent": "ktrade-agent", "Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            ts = str(int(self.clock()))
            sig = hmac.new(self.secret.encode(), (method + ts + path + query + payload).encode(), hashlib.sha256).hexdigest()
            headers |= {"api-key": self.key, "timestamp": ts, "signature": sig}
        try:
            res = self.http.request(method, self.base_url + path + query, data=payload or None, headers=headers, timeout=15)
            out = res.json()
        except (requests.RequestException, ValueError) as e:
            raise ExchangeError(f"{method} {path}: {e}") from e
        if res.status_code >= 400 or not out.get("success", False):
            raise ExchangeError(f"{method} {path} -> HTTP {res.status_code}: {json.dumps(out.get('error') or out)[:300]}")
        return out

    # --- market ----------------------------------------------------------------------------------
    def products(self) -> Dict[str, Dict[str, Any]]:
        if not self._products:
            rows = self._request("GET", "/v2/products", {"contract_types": "perpetual_futures", "page_size": 500}, auth=False)["result"]
            self._products = {p["symbol"]: {"id": p["id"], "contract_value": float(p["contract_value"]),
                                            "tick": float(p["tick_size"]), "max_leverage": float(p.get("default_leverage") or 10)}
                              for p in rows if p.get("state") == "live"}
        return self._products

    def quotes(self) -> Dict[str, Dict[str, float]]:
        out = {}
        for t in self._request("GET", "/v2/tickers", {"contract_types": "perpetual_futures"}, auth=False)["result"]:
            q = t.get("quotes") or {}
            bid, ask = float(q.get("best_bid") or 0), float(q.get("best_ask") or 0)
            if bid and ask and ask >= bid:
                out[t["symbol"]] = {"bid": bid, "ask": ask, "mark": float(t.get("mark_price") or (bid + ask) / 2)}
        return out

    def round_price(self, symbol: str, price: float) -> str:
        tick = self.products()[symbol]["tick"] or 0.01
        decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
        return f"{round(round(price / tick) * tick, decimals):.{decimals}f}"

    # --- account ---------------------------------------------------------------------------------
    def equity(self) -> Dict[str, float]:
        out = self._request("GET", "/v2/wallet/balances")
        usd = next((b for b in out["result"] if b.get("asset_symbol") in ("USD", "USDT")), {})
        return {"equity": float((out.get("meta") or {}).get("net_equity") or usd.get("balance") or 0),
                "available": float(usd.get("available_balance") or 0)}

    def positions(self) -> List[Dict[str, Any]]:
        rows = self._request("GET", "/v2/positions/margined", {"contract_types": "perpetual_futures"})["result"]
        out = []
        for p in rows:
            size = int(float(p.get("size") or 0))
            if size:
                out.append({"symbol": p["product_symbol"], "product_id": p["product_id"], "size": abs(size),
                            "side": "long" if size > 0 else "short", "entry": float(p["entry_price"]),
                            "mark": float(p.get("mark_price") or 0), "unrealized": float(p.get("unrealized_pnl") or 0),
                            "liquidation": float(p.get("liquidation_price") or 0)})
        return out

    def stop_orders(self) -> Dict[str, Dict[str, float]]:
        """Open stop-loss / take-profit trigger prices per symbol."""
        rows = self._request("GET", "/v2/orders", {"states": "open,pending"})["result"]
        out: Dict[str, Dict[str, float]] = {}
        for o in rows:
            kind = o.get("stop_order_type")
            if kind in ("stop_loss_order", "take_profit_order") and o.get("stop_price"):
                out.setdefault(o["product_symbol"], {})["stop" if kind == "stop_loss_order" else "target"] = float(o["stop_price"])
        return out

    def fills(self, since_us: Optional[int] = None) -> List[Dict[str, Any]]:
        params = {"page_size": 100} | ({"start_time": since_us} if since_us else {})
        return self._request("GET", "/v2/fills", params)["result"]

    # --- trading ---------------------------------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: float) -> None:
        pid = self.products()[symbol]["id"]
        self._request("POST", f"/v2/products/{pid}/orders/leverage", body={"leverage": str(int(leverage))})

    def market(self, symbol: str, side: str, size: int, reduce_only: bool = False) -> Dict[str, Any]:
        body = {"product_id": self.products()[symbol]["id"], "size": int(size), "side": side,
                "order_type": "market_order", "reduce_only": reduce_only}
        return self._request("POST", "/v2/orders", body=body)["result"]

    def protect(self, symbol: str, stop: float, target: Optional[float]) -> None:
        """Exchange-held bracket for the whole position: stop-loss always, take-profit when given."""
        body: Dict[str, Any] = {"product_id": self.products()[symbol]["id"], "bracket_stop_trigger_method": "last_traded_price",
                                "stop_loss_order": {"order_type": "market_order", "stop_price": self.round_price(symbol, stop)}}
        if target:
            body["take_profit_order"] = {"order_type": "market_order", "stop_price": self.round_price(symbol, target)}
        self._request("POST", "/v2/orders/bracket", body=body)

    def cancel_all(self, symbol: str) -> None:
        self._request("DELETE", "/v2/orders/all", body={"product_id": self.products()[symbol]["id"],
                                                        "cancel_limit_orders": True, "cancel_stop_orders": True})

    def close(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Market-close the whole position and remove its stop orders."""
        pos = next((p for p in self.positions() if p["symbol"] == symbol), None)
        if not pos:
            return None
        res = self.market(symbol, "sell" if pos["side"] == "long" else "buy", pos["size"], reduce_only=True)
        try:
            self.cancel_all(symbol)
        except ExchangeError:
            pass                                   # the reduce-only close already removed the risk
        return res
