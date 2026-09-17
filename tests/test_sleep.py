"""Regression tests for night mode (sleep: follow_sway / idle_timeout)."""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from razerdash.config import ConfigError, Sleep, parse_config
from razerdash.daemon import Daemon
from razerdash.explain import describe_config
from razerdash.sleep import (InputIdleWatcher, SleepController, SwayWatcher,
                             _fmt_duration)

MINIMAL = {
    "bindings": [{
        "name": "a", "metric": "up",
        "key_group": {"type": "row", "index": 0},
        "lighting": {"method": "fill_fixed", "color": "#00e5ff"},
    }],
}


def cfg(sleep=None):
    raw = dict(MINIMAL)
    if sleep is not None:
        raw["sleep"] = sleep
    return parse_config(raw)


# 1. config parsing
c = cfg()
assert c.sleep == Sleep(follow_sway=True, idle_timeout=0.0), c.sleep
c = cfg({"follow_sway": False, "idle_timeout": "20m"})
assert c.sleep == Sleep(follow_sway=False, idle_timeout=1200.0), c.sleep
for bad in ({"idle_timeout": -5}, "yes", ["x"]):
    try:
        cfg(bad)
        raise AssertionError(f"accepted sleep: {bad!r}")
    except ConfigError:
        pass
print("1. sleep config parsing (defaults, 20m, rejects bad values): OK")

# 2. duration formatting used in the journal reason
assert _fmt_duration(1200) == "20m" and _fmt_duration(7200) == "2h"
assert _fmt_duration(45) == "45s" and _fmt_duration(90) == "90s"
print("2. duration formatting: OK")

# 3. live sway IPC on this machine: monitors are on right now
on = SwayWatcher().outputs_on()
assert on is True, f"expected outputs_on() True with monitors on, got {on!r}"
print("3. SwayWatcher.outputs_on() against live sway: OK (True)")

# 4. SwayWatcher with nothing to talk to -> None, never darkens
w = SwayWatcher()
w._candidates = lambda: []
assert w.outputs_on() is None
print("4. SwayWatcher without a socket -> None: OK")

# 5. idle watcher: returns None (no permission) or a small non-negative float
idle = InputIdleWatcher().seconds_idle()
assert idle is None or (isinstance(idle, float) and idle >= 0), idle
readable = "readable" if idle is not None else "NOT readable (no input group)"
print(f"5. InputIdleWatcher.seconds_idle(): OK ({readable})")

# 6. controller: both triggers disabled -> always None
ctl = SleepController(Sleep(follow_sway=False, idle_timeout=0))
assert ctl.status() is None and ctl._sway is None and ctl._idle is None
print("6. controller with everything disabled: OK")


def fresh(ctl):  # bypass the 1s result cache
    ctl._next_check = 0.0
    return ctl.status()


# 7. controller reacts to monitor power (sway watcher stubbed)
ctl = SleepController(Sleep(follow_sway=True, idle_timeout=0))
ctl._sway.outputs_on = lambda: False
assert fresh(ctl) == "all monitors off"
ctl._sway.outputs_on = lambda: True
assert fresh(ctl) is None
ctl._sway.outputs_on = lambda: None  # sway gone: never darkens
assert fresh(ctl) is None
print("7. controller follows monitor power, inert without sway: OK")

# 8. controller reacts to input idleness (idle watcher stubbed)
ctl = SleepController(Sleep(follow_sway=False, idle_timeout=300))
assert ctl._idle is not None
ctl._idle.seconds_idle = lambda: 301.0
assert fresh(ctl) == "no input for 5m"
ctl._idle.seconds_idle = lambda: 10.0
assert fresh(ctl) is None
ctl._idle.seconds_idle = lambda: None  # unreadable devices: never darkens
assert fresh(ctl) is None
print("8. controller follows idle_timeout, inert without /dev/input: OK")

# 9. daemon rebuilds the controller only when the sleep config changes
d = Daemon(watcher=None, backend=None)
d._night(types.SimpleNamespace(sleep=Sleep(follow_sway=False, idle_timeout=0)))
first = d._sleepctl
d._night(types.SimpleNamespace(sleep=Sleep(follow_sway=False, idle_timeout=0)))
assert d._sleepctl is first
d._night(types.SimpleNamespace(sleep=Sleep(follow_sway=False, idle_timeout=60)))
assert d._sleepctl is not first
d._sleepctl.close()
print("9. daemon keeps/rebuilds the controller correctly: OK")

# 10. explain mentions the sleep policy
out = describe_config(cfg({"follow_sway": True, "idle_timeout": 0}))
assert "dark while all monitors are off" in out, out
out = describe_config(cfg({"follow_sway": False, "idle_timeout": 0}))
assert "never -- LEDs stay on" in out, out
print("10. explain shows the sleep line: OK")

print("\nall sleep regressions passed")
