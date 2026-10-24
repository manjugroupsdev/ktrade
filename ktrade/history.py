"""
Historical hourly candles for research, from Binance's public market-data mirror (no key needed).

Delta Exchange India only exposes about a year of history, so research uses Binance USDT spot prices for the
same coins. Prices track closely; Delta's own fees, slippage and funding are applied in the backtest.
"""
import os
import time

import pandas as pd
import requests

MIRROR = "https://data-api.binance.vision/api/v3/klines"
CACHE = os.path.join("data", "history")

# Large, liquid coins that traded throughout 2021-2026 and are listed on Delta India.
# Survivors only (no LUNA or FTT), which flatters any long-biased result; keep that in mind.
UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK", "LTC", "BCH",
            "AVAX", "DOT", "UNI", "ATOM", "TRX", "ETC", "FIL", "NEAR", "AAVE", "ZEC"]


def hourly(coin: str, start: str = "2021-01-01", session=None, refresh_after_s: float = 86400) -> pd.DataFrame:
    """Closed 1-hour candles (time = bar open, seconds, UTC), cached under data/history."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{coin}USDT_1h.csv.gz")
    cached = pd.read_csv(path) if os.path.exists(path) else None
    if cached is not None and time.time() - os.path.getmtime(path) < refresh_after_s:
        return cached
    http = session or requests.Session()
    since = int(cached["time"].iloc[-1] * 1000) + 1 if cached is not None and len(cached) else \
        int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    rows = []
    while True:
        batch = http.get(MIRROR, params={"symbol": f"{coin}USDT", "interval": "1h", "startTime": since, "limit": 1000},
                         timeout=30).json()
        if not isinstance(batch, list) or not batch:
            break
        rows += batch
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    new = pd.DataFrame([r[:6] for r in rows], columns=["time", "open", "high", "low", "close", "volume"])
    new = new.astype({"time": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})
    new["time"] //= 1000
    df = pd.concat([cached, new]) if cached is not None else new
    df = df.drop_duplicates("time").sort_values("time")
    df = df[df["time"] + 3600 <= time.time()].reset_index(drop=True)          # closed bars only
    df.to_csv(path, index=False, compression="gzip")
    return df


def resample(df: pd.DataFrame, hours: int) -> pd.DataFrame:
    if hours == 1:
        return df
    sec = hours * 3600
    g = df.assign(bucket=df["time"] // sec * sec).groupby("bucket")
    out = pd.DataFrame({"time": g["time"].first().index, "open": g["open"].first().values, "high": g["high"].max().values,
                        "low": g["low"].min().values, "close": g["close"].last().values, "volume": g["volume"].sum().values,
                        "n": g["time"].count().values})
    return out[out["n"] == hours].drop(columns="n").reset_index(drop=True)      # only complete bars
