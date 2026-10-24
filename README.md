# ktrade: Claude vs Rules

A crypto paper-trading lab that answers one question honestly: **does an LLM make better trade decisions
than simple rules?**

Every hour, a scanner picks the liquid coins on Delta Exchange India whose volatility is expanding in a
clear direction. Two books then trade the same candidates with the same risk engine:

| Book | Who decides the entry | Who decides size, stop, target, limits |
|---|---|---|
| **AI** | Claude (via the Claude Code CLI, optionally with live web search for news) | the risk engine |
| **Baseline** | a fixed rule: trade the direction of the trend score when it is strong | the risk engine |

The dashboard shows both equity curves side by side, every decision and every refusal, so after a few weeks
you can see whether the AI added anything beyond the rules. **It is paper trading only.** There is no code
that places real orders.

> **Not financial advice.** Most traders lose money. Past paper results do not predict future returns.
> Paper fills ignore order-book depth and real-world outages. Treat this as research software.

## How it works

```
every 30 s   live prices → close positions at their stop, target or 48 h time limit
every hour   scanner: liquid (≥ $5M/day), listed ≥ 60 days, tight spread, not parabolic
               → top 5 by (volatility expansion, capped at 3x) × |trend score|
             ├─ Baseline: trend ≥ +0.5 → long, ≤ −0.5 → short
             └─ AI:       Claude reads the same table (+ web news) → long / short / skip
                          with a conviction; below 0.6 is skipped. Claude can also exit AI-book positions.
             → risk engine (both books): 0.5% of equity at risk per trade including fees and slippage,
               stop = 3 × ATR(1h), target = 2 × stop, max 3 positions, max 2x gross exposure,
               no new entries after −3% in a day (IST)
```

- **Trend score** (−1 to +1): average of four classic trend models on 1-hour candles: 50/200 EMA,
  55/20 and 168/72 Donchian breakouts, and 7-day momentum.
- **Too wild**: coins with 1-hour ATR above 3% or a 24-hour move above 25% are skipped and listed separately.
  These are usually pump-and-dumps where stops are meaningless.
- **Costs**: taker fee 0.05% plus 18% GST each side, 3 bps slippage, and perpetual funding charged or paid
  per 8 hours at the rate seen when the position opened.
- **Claude** runs in a clean headless session (`claude -p` with no MCP servers, plugins or settings, and only
  the WebSearch tool) and must answer in a JSON schema that only allows the symbols it was shown. If Claude
  is unavailable, the AI book simply takes no new trades that hour.

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

One Claude call per hour when there are candidates or open AI positions: about 24 calls a day, each roughly
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
| `ktrade/app.py`, `ktrade/static/index.html` | read-only dashboard |

## Legal notes (India)

Crypto derivatives on FIU-registered exchanges such as Delta Exchange India are accessible to Indian
residents. Crypto profits are taxed at 30% with no loss set-off, plus 1% TDS on transfers. Offering trading
signals or managing other people's money can require SEBI or other registration depending on the asset and
the service. This project only simulates trades on public prices. Check the rules that apply to you before
trading real money.

## License

MIT
