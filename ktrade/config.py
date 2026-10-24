"""Settings, read from KTRADE_* environment variables (and .env). Every default is a paper-trading default."""
import os
from dataclasses import dataclass, fields

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    delta_base_url: str = "https://api.india.delta.exchange"
    db_path: str = "data/ktrade.db"
    start_equity: float = 1000.0

    # Universe: liquid perpetuals that have traded long enough to have a history (skips fresh pump listings).
    min_turnover_usd: float = 5_000_000
    min_listing_days: int = 60
    max_spread_bps: float = 15.0
    exclude: tuple = ()
    max_candidates: int = 5
    # "Too wild" filter: parabolic pumps and crashes are skipped, not ranked first.
    max_atr_pct: float = 3.0               # 1-hour ATR above 3% of price
    max_move_24h_pct: float = 25.0         # moved more than 25% either way in 24 hours
    vol_ratio_cap: float = 3.0             # volatility expansion counts at most 3x in the score
    max_correlation: float = 0.85          # two coins moving this much in step are the same bet (e.g. XAUT and PAXG)

    # Bars and exits, chosen by scripts/research.py (4h trailing-stop rules; see docs/RESEARCH.md).
    bar_hours: int = 4                     # scan on 4-hour candles; 1-hour rules lost money out of sample
    trail: bool = True                     # the stop follows the close by stop_atr x ATR and never loosens
    trend_exit: bool = True                # exit when the trend score no longer supports the position
    long_only: bool = False

    # Risk engine. Applies to both books; Claude can never change these.
    risk_per_trade: float = 0.005          # 0.5% of equity lost if the stop is hit (plus costs)
    max_positions: int = 3
    max_gross_leverage: float = 2.0        # total notional across positions / equity
    daily_loss_limit: float = 0.03         # no new entries for the rest of the IST day after -3%
    stop_atr: float = 3.0                  # stop distance = 3 x ATR(14) of the bars
    take_profit_r: float = 0.0             # fixed target as a multiple of the stop distance; 0 = none (let winners run)
    max_hold_hours: float = 0.0            # 0 = no time limit
    slippage_bps: float = 3.0
    taker_fee: float = 0.0005 * 1.18       # Delta taker fee + 18% GST
    min_conviction: float = 0.6            # AI book only: weaker ideas are skipped
    baseline_trend: float = 0.5            # baseline book: |trend score| needed to trade

    # Claude CLI (uses your own Claude subscription/login)
    claude_bin: str = "claude"
    claude_model: str = "sonnet"
    claude_timeout: int = 240
    claude_web: bool = True

    # Dashboard
    host: str = "127.0.0.1"
    port: int = 8060
    panel_token: str = ""                  # required for requests that arrive through a proxy/tunnel

    @classmethod
    def from_env(cls, env_file: str = ".env") -> "Settings":
        load_dotenv(env_file)
        values = {}
        for f in fields(cls):
            raw = os.environ.get(f"KTRADE_{f.name.upper()}")
            if raw is None:
                continue
            default = f.default
            if isinstance(default, bool):
                values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, tuple):
                values[f.name] = tuple(s.strip().upper() for s in raw.split(",") if s.strip())
            else:
                values[f.name] = type(default)(raw)
        return cls(**values)
