"""
Claude as the trade proposer, through the Claude Code CLI (`claude -p`), using your own login.

Claude sees the scanner's candidates and the AI book's open positions, may search the web for fresh news,
and answers long / short / skip per candidate plus hold / exit per open position. It never sets size,
stops or limits: the risk engine does, identically for the AI book and the rules-only baseline.
"""
import json
import subprocess
from typing import Any, Callable, Dict, List

from ktrade.config import Settings

SYSTEM = """You are the trade proposer in a paper-trading experiment on Delta Exchange India crypto perpetuals.
Your results are compared, trade by trade, against a simple rules-only baseline that sees the same candidates.
You do NOT set position size, stop-loss, target or leverage; a fixed risk engine does. You can only say:
- per candidate: long, short or skip, with a conviction from 0 to 1 (below 0.6 is treated as skip)
- per open position: hold or exit
Skipping is often the best answer. Fees + slippage cost about 0.15% per round trip, so tiny edges lose money.
Prefer trades where expanding volatility agrees with the trend and with fresh news. Use web search only when
recent news could change the decision (listings, hacks, unlocks, regulation, macro events). Be concise and concrete."""


def schema(candidates: List[str], positions: List[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "market_view": {"type": "string", "maxLength": 400},
            "entries": {"type": "array", "items": {"type": "object", "properties": {
                "symbol": {"type": "string", "enum": candidates or ["NONE"]},
                "action": {"type": "string", "enum": ["long", "short", "skip"]},
                "conviction": {"type": "number", "minimum": 0, "maximum": 1},
                "thesis": {"type": "string", "maxLength": 400},
                "invalidation": {"type": "string", "maxLength": 200},
            }, "required": ["symbol", "action", "conviction", "thesis"]}},
            "exits": {"type": "array", "items": {"type": "object", "properties": {
                "symbol": {"type": "string", "enum": positions or ["NONE"]},
                "action": {"type": "string", "enum": ["hold", "exit"]},
                "reason": {"type": "string", "maxLength": 300},
            }, "required": ["symbol", "action", "reason"]}},
        },
        "required": ["market_view", "entries", "exits"],
    }


class CouncilError(RuntimeError):
    pass


class ClaudeCouncil:
    def __init__(self, s: Settings, run: Callable = subprocess.run):
        self.s, self.run = s, run

    def command(self, prompt: str, sch: Dict[str, Any]) -> List[str]:
        cmd = [self.s.claude_bin, "-p", prompt, "--model", self.s.claude_model, "--output-format", "json",
               "--json-schema", json.dumps(sch), "--system-prompt", SYSTEM, "--no-session-persistence",
               # A clean, cheap session: none of the user's MCP servers, plugins or settings.
               "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", ""]
        tools = ["WebSearch"] if self.s.claude_web else []
        return cmd + (["--tools", *tools, "--allowedTools", *tools] if tools else ["--tools", ""])

    def decide(self, candidates: List[Dict[str, Any]], positions: List[Dict[str, Any]], equity: float) -> Dict[str, Any]:
        cand = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in c.items() if k != "contract_value"} for c in candidates]
        pos = [{"symbol": p["symbol"], "side": p["side"], "entry": p["entry"], "mark": p.get("mark"),
                "unrealized_usd": p.get("unrealized"), "hours_open": p.get("hours_open"), "opened_because": p.get("reason")}
               for p in positions]
        exits = (f"Positions exit by a trailing stop {self.s.stop_atr:g}x ATR behind the close, or when the trend score "
                 f"turns against them{'' if not self.s.take_profit_r else f', or at a {self.s.take_profit_r:g}R target'}. "
                 "You may also exit AI-book positions early.")
        prompt = (f"Paper equity: ${equity:,.2f}. {exits}\n\nCandidates ({self.s.bar_hours}-hour candles; trend -1..+1 from four trend models; "
                  f"vol_ratio = ATR now / 30-day median ATR; score = vol_ratio x |trend|):\n{json.dumps(cand, indent=1)}\n\n"
                  f"Open AI-book positions:\n{json.dumps(pos, indent=1) if pos else 'none'}\n\n"
                  "Decide per candidate and per open position. Reply with the JSON schema.")
        sch = schema([c["symbol"] for c in candidates], [p["symbol"] for p in positions])
        try:
            done = self.run(self.command(prompt, sch), capture_output=True, text=True, timeout=self.s.claude_timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CouncilError(f"claude CLI did not answer: {e}") from e
        try:
            out = json.loads(done.stdout)
        except ValueError as e:
            raise CouncilError(f"claude CLI returned non-JSON (exit {done.returncode}): {(done.stderr or done.stdout)[:300]}") from e
        if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
            raise CouncilError(f"claude CLI error: {str(out.get('result') or out)[:300]}")
        return self.validate(out["structured_output"], candidates, positions) | {
            "cost_usd": float(out.get("total_cost_usd") or 0), "duration_ms": out.get("duration_ms")}

    @staticmethod
    def validate(d: Dict[str, Any], candidates: List[Dict], positions: List[Dict]) -> Dict[str, Any]:
        """Keep only well-formed answers about symbols Claude was actually shown."""
        known, held = {c["symbol"] for c in candidates}, {p["symbol"] for p in positions}
        entries = [{"symbol": e["symbol"], "action": e["action"], "conviction": max(0.0, min(1.0, float(e.get("conviction", 0)))),
                    "thesis": str(e.get("thesis", ""))[:400], "invalidation": str(e.get("invalidation", ""))[:200]}
                   for e in d.get("entries", []) if e.get("symbol") in known and e.get("action") in ("long", "short", "skip")]
        exits = [{"symbol": x["symbol"], "action": x["action"], "reason": str(x.get("reason", ""))[:300]}
                 for x in d.get("exits", []) if x.get("symbol") in held and x.get("action") in ("hold", "exit")]
        return {"market_view": str(d.get("market_view", ""))[:400], "entries": entries, "exits": exits}
