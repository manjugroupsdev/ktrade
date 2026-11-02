from ktrade.uptime import Uptime, summary


def test_alive_time_sleep_restart_and_network_outage(ledger, clock):
    up = Uptime(ledger, clock)
    for _ in range(10):                                                  # 5 minutes alive
        clock.t += 30; up.beat(True)
    clock.t += 3600; up.beat(True)                                       # Mac slept for an hour
    clock.t += 30; up.beat(False)                                        # Wi-Fi lost
    clock.t += 60; up.beat(False)
    clock.t += 30; up.beat(True)                                         # back
    s = summary(ledger.uptime_rows(), clock.t)
    assert s["running"] and s["online"]
    assert abs(s["alive_s"] - 420) < 1 and abs(s["offline_s"] - 90) < 1 and abs(s["down_s"] - 3600) < 1
    kinds = [e["kind"] for e in s["events"]]
    assert kinds.count("resume") == 1 and kinds.count("offline") == 1 and kinds.count("online") == 1

    clock.t += 30; up.beat(False)                                        # offline right now
    assert not summary(ledger.uptime_rows(), clock.t)["online"]
    clock.t += 7200
    Uptime(ledger, clock)                                                # service restarted 2 hours later
    s = summary(ledger.uptime_rows(), clock.t)
    assert s["restarts"] == 2 and s["online"] and s["events"][0]["kind"] == "restart"
    assert summary(ledger.uptime_rows(), clock.t + 600)["running"] is False
