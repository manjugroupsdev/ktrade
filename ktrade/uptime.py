"""
How long the agent has been alive, and every start, resume and outage, for the dashboard.

The agent loop calls `beat()` every 30 s. A beat that arrives much later than expected means the process was frozen
(the Mac slept or the lid was closed); a fresh process means the service was stopped, crashed or the Mac restarted.
Exchange calls failing marks the agent offline (no network, or Delta unreachable) until they succeed again.
"""
import time
from typing import Any, Dict, List, Optional

GAP = 120                     # seconds without a beat that count as "not running"
DOWN = "network or exchange unreachable"                      # note of an outage that has not ended yet


class Uptime:
    def __init__(self, ledger, clock=time.time):
        self.ledger, self.clock = ledger, clock
        now = clock()
        self.run = ledger.uptime_open("run", now, "started")
        self.beat_at, self.offline = now, None

    def beat(self, online: bool) -> None:
        now = self.clock()
        if now - self.beat_at > GAP:                          # frozen: the Mac was asleep
            if self.offline:
                self.ledger.uptime_close(self.offline, self.beat_at, "ended by sleep")
                self.offline = None
            self.run = self.ledger.uptime_open("run", now, "resumed after sleep")
        self.ledger.uptime_close(self.run, now)
        if not online and not self.offline:
            self.offline = self.ledger.uptime_open("offline", now, DOWN)
        elif self.offline:
            self.ledger.uptime_close(self.offline, now, None if not online else "back online")
            if online:
                self.offline = None
        self.beat_at = now


def summary(rows: List[Dict[str, Any]], now: float, day: float = 86400) -> Dict[str, Any]:
    runs = [r for r in rows if r["kind"] == "run"]
    offs = [r for r in rows if r["kind"] == "offline"]
    if not runs:
        return {"available": False}
    first, cur = runs[0]["started"], runs[-1]
    running = now - cur["ended"] <= GAP
    alive = sum(r["ended"] - r["started"] for r in runs)
    offline = sum(r["ended"] - r["started"] for r in offs)
    events: List[Dict[str, Any]] = []
    prev: Optional[Dict[str, Any]] = None
    for r in runs:
        if prev is None:
            events.append({"at": r["started"], "kind": "start", "text": "First started"})
        elif r["note"] == "resumed after sleep":
            events.append({"at": r["started"], "kind": "resume", "text": "Resumed after the Mac slept", "gap": r["started"] - prev["ended"]})
        else:
            events.append({"at": r["started"], "kind": "restart", "text": "Restarted (service was off)", "gap": r["started"] - prev["ended"]})
        prev = r
    out_now = running and any(r["note"] == DOWN and r["ended"] >= cur["started"] for r in offs)
    for r in offs:
        events.append({"at": r["started"], "kind": "offline", "text": "Lost network / exchange"})
        if r["note"] == "back online":
            events.append({"at": r["ended"], "kind": "online", "text": "Back online", "gap": r["ended"] - r["started"]})
    if not running:
        events.append({"at": cur["ended"], "kind": "stop", "text": "Stopped or asleep"})
    since = now - day                                          # timeline: alive / offline segments in the last day
    seg = [{"from": max(r["started"], since), "to": r["ended"], "state": "up"} for r in runs if r["ended"] > since]
    seg += [{"from": max(r["started"], since), "to": r["ended"], "state": "offline"} for r in offs if r["ended"] > since]
    return {"available": True, "now": now, "first_start": first, "running": running,
            "online": running and not out_now,
            "session_start": cur["started"], "alive_s": alive, "offline_s": offline, "down_s": max(0.0, now - first - alive),
            "uptime_pct": round(alive / max(1.0, now - first) * 100, 1), "restarts": len(runs) - 1,
            "events": sorted(events, key=lambda e: e["at"], reverse=True)[:40], "timeline": seg, "timeline_from": since}
