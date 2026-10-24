# ktrade: Claude vs Rules

A crypto paper-trading lab that answers one question honestly: **does an LLM make better trade decisions
than simple rules?**

Every 4 hours, a scanner picks the liquid coins on Delta Exchange India whose volatility is expanding in a
clear direction. Two books then trade the same candidates with the same risk engine and the same exits:

| Book | Who decides the entry | Who decides size, stop, exits and limits |
|---|---|---|
| **AI** | Claude (via the Claude Code CLI, optionally with live web search for news) | the risk engine |
| **Baseline** | a fixed rule: trade the direction of the trend score when it is strong | the risk engine |

The dashboard shows both equity curves side by side, every decision and every refusal, and the historical
research behind the rules, so you can see whether the AI adds anything beyond the rules. **It is paper trading
only.** There is no code that places real orders.

> **Not financial advice.** Most traders lose money. Past results, simulated or historical, do not predict
> future returns. Paper fills ignore order-book depth and real-world outages. Treat this as research software.

## How it works

```
every 30 s     live prices → close any position that hits its stop
every 4 hours  (after the candle closes)
               1. review open positions: trail each stop to 3 × ATR behind the close (it only tightens),
                  and exit any position whose trend score no longer supports it
               2. scanner: liquid (≥ $5M/day), listed ≥ 60 days, tight spread, not parabolic,
                  not the same bet as a coin already chosen (7-day return correlation > 0.85)
                  → top 5 by (volatility expansion, capped at 3x) × |trend score|
               3. Baseline: trend ≥ +0.5 → long, ≤ −0.5 → short
                  AI:       Claude reads the same table (+ web news) → long / short / skip with a conviction;
                            below 0.6 is skipped. Claude may also exit AI-book positions early.
               4. risk engine (both books): 0.5% of equity at risk per trade including fees and slippage,
                  initial stop 3 × ATR, no fixed target, max 3 positions, max 2x gross exposure,
                  no new entries after −3% in a day (IST)
```

- **Trend score** (−1 to +1): average of four classic trend models: 50/200 EMA, 55/20 and 168/72 Donchian
  breakouts, and 168-bar momentum.
- **Too wild**: coins with an ATR above 3% × √(bar hours) of price or a 24-hour move above 25% are skipped.
  These are usually pump-and-dumps where stops are meaningless.
- **Costs**: taker fee 0.05% plus 18% GST each side, 3 bps slippage, and perpetual funding.
- **Claude** runs in a clean headless session (`claude -p` with no MCP servers, plugins or settings, and only
  the WebSearch tool) and must answer in a JSON schema that only allows the symbols it was shown. If Claude
  is unavailable, the AI book simply takes no new trades that cycle.

## Research: why these rules

`scripts/research.py` replays the rules book bar by bar on 20 major coins (2021 to now, Binance hourly prices
standing in for Delta), with the same scanner, risk engine, fees, slippage and funding. Seven variants were fixed in
advance. The pick was made on **2021–2023 only** and then checked on **2024 onwards**, which the pick never saw:

| Rules | 2021–23 return | Sharpe | 2024–now return | worst drop | Sharpe | won per $1 lost |
|---|---|---|---|---|---|---|
| 1-hour bars, 2R target, 48 h limit (v0.1) | +12% | 0.30 | **−34%** | −39% | −0.63 | 0.93 |
| 1-hour bars, trailing stop | −7% | 0.01 | −32% | −48% | −0.52 | 0.93 |
| 4-hour bars, 2R target | +36% | 1.10 | +10% | −11% | 0.39 | 1.06 |
| **4-hour bars, trailing stop, trend exit (live)** | **+57%** | **1.39** | **+33%** | **−13%** | **0.91** | **1.26** |
| 4-hour, trailing, long only | +25% | 0.77 | +7% | −16% | 0.27 | 1.07 |
| Daily bars, trailing | +2% | 0.20 | +0.4% | −6% | 0.06 | 1.18 |

- Short-term (1-hour) trading lost money once fees and noise were counted, and fixed profit targets cut off
  the big trend moves that pay for the losers.
- Every neighbouring setting of the chosen rules (stop 2–4 × ATR, trend threshold 0.5 or 0.75) was also
  profitable in both periods, from +10% to +33% out of sample. The chosen stop is the best of them, so expect
  the middle of that range rather than the top.
- Buy and hold made more over this period (BTC +84% and a 20-coin basket +210% since 2024) but fell 53–86% at
  its worst. These rules aim for steadier returns with drops of about 15%.
- Caveats: survivor coins only (no LUNA or FTT), Binance spot prices instead of Delta perpetuals, fractional
  quantities, and one fixed set of variants.

Rerun it any time with `.venv/bin/python scripts/research.py`. The dashboard reads the newest report.

## Quick start

Requirements: Python 3.11+, and the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) logged in (`claude` works in your terminal).

```bash
git clone https://github.com/manjugroupsdev/ktrade.git && cd ktrade
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # optional: adjust settings
.venv/bin/python -m pytest      # offline tests, no network or Claude needed
.venv/bin/python -m ktrade scan # see what the scanner finds right now
.venv/bin/python -m ktrade once # one full cycle, including one Claude call
.venv/bin/python -m ktrade run  # keep running; dashboard at http://127.0.0.1:8060
```

### Run it 24/7 on a Mac

```bash
sh scripts/install_macos.sh            # launchd service: starts at login, restarts on crash, keeps the Mac awake
sh scripts/install_macos.sh uninstall
```

Keep the Mac plugged in with the lid open. macOS sleeps on lid close, and a sleeping bot can't watch its stops.

### Linux (systemd)

```ini
# ~/.config/systemd/user/ktrade.service
[Service]
WorkingDirectory=%h/ktrade
ExecStart=%h/ktrade/.venv/bin/python -m ktrade run
Restart=always
[Install]
WantedBy=default.target
```

Then run `systemctl --user enable --now ktrade`.

## Claude usage

One Claude call per 4-hour cycle when there are candidates or open AI positions: about 6 calls a day, each roughly
5–10k tokens plus any web searches. This uses your own Claude login and counts against your plan's limits.
Set `KTRADE_CLAUDE_WEB=false` to disable web search, or `KTRADE_CLAUDE_MODEL` to pick another model.

## Reading the results

- Compare the books only after **about 30 closed trades each**. Before that, the difference is mostly luck.
- Look at profit factor (won per $1 lost), worst drawdown and fees, not just the final number.
- The decision log keeps every idea, including refusals, so you can check Claude's reasons against what happened.

## Layout

| File | What it does |
|---|---|
| `ktrade/delta.py` | Delta India public market data (products, tickers, candles), with retries |
| `ktrade/signals.py` | ATR, RSI, trend score and volatility features from closed candles |
| `ktrade/scanner.py` | universe filters, "too wild" filter and ranking |
| `ktrade/council.py` | Claude CLI adapter: prompt, JSON schema and validation |
| `ktrade/risk.py` | position sizing and daily loss limit |
| `ktrade/ledger.py` | SQLite paper ledger: positions, trades, fees, funding, decisions, equity curve |
| `ktrade/engine.py` | the loop that ties it together |
| `ktrade/history.py`, `ktrade/backtest.py`, `scripts/research.py` | historical data, portfolio backtest and the research report |
| `ktrade/app.py`, `ktrade/static/index.html` | read-only dashboard |

## Legal notes (India)

Crypto derivatives on FIU-registered exchanges such as Delta Exchange India are accessible to Indian
residents. Crypto profits are taxed at 30% with no loss set-off, plus 1% TDS on transfers. Offering trading
signals or managing other people's money can require SEBI or other registration depending on the asset and
the service. This project only simulates trades on public prices. Check the rules that apply to you before
trading real money.

## License

MIT
