"""The risk engine. Identical for both books; nothing here can be changed by Claude."""
import math
from dataclasses import dataclass
from typing import Union

from ktrade.config import Settings


@dataclass
class Plan:
    symbol: str
    side: int            # +1 long, -1 short
    qty: int             # contracts
    entry: float
    stop: float
    target: float
    risk_usd: float      # loss at the stop, including round-trip fees and slippage
    notional: float
    contract_value: float


def plan_entry(s: Settings, symbol: str, side: int, price: float, atr: float, contract_value: float,
               equity: float, gross_notional: float) -> Union[Plan, str]:
    """A sized trade, or the reason it is refused."""
    if price <= 0 or atr <= 0 or equity <= 0:
        return "no usable price or volatility"
    stop_dist = s.stop_atr * atr
    costs = (2 * s.taker_fee + 2 * s.slippage_bps / 1e4) * price
    per_contract = (stop_dist + costs) * contract_value
    budget = equity * s.risk_per_trade
    qty = math.floor(budget / per_contract)
    if qty < 1:
        return f"one contract would risk ${per_contract:,.2f}, above the ${budget:,.2f} budget"
    room = s.max_gross_leverage * equity - gross_notional
    qty = min(qty, math.floor(room / (price * contract_value)) if room > 0 else 0)
    if qty < 1:
        return f"exposure cap: {s.max_gross_leverage:g}x equity already used"
    return Plan(symbol, side, qty, price, price - side * stop_dist, price + side * s.take_profit_r * stop_dist,
                round(qty * per_contract, 2), round(qty * price * contract_value, 2), contract_value)


def daily_loss_hit(s: Settings, day_start_equity: float, equity: float) -> bool:
    return day_start_equity > 0 and (day_start_equity - equity) / day_start_equity >= s.daily_loss_limit
