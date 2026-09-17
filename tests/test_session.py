"""Regression tests for anchored session windows (session: / ${session})."""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from razerdash.config import ConfigError, Session, parse_config
from razerdash.render import binding_value
from razerdash.session import MIN_WINDOW, SessionTracker

H = 3600.0


def cfg(binding_extra):
    raw = {"bindings": [dict({
        "name": "s", "metric": "m",
        "key_group": {"type": "row", "index": 0},
        "lighting": {"method": "fill_fixed", "color": "#00e5ff"},
    }, **binding_extra)]}
    return parse_config(raw).bindings[0]


class FakeProm:
    def __init__(self, range_points=(), instant=()):
        self.range_points = list(range_points)
        self.instant = list(instant)
        self.queries = []

    def query(self, q):
        self.queries.append(q)
        return list(self.instant)

    def query_range(self, q, start, end, step):
        return [(t, v) for t, v in self.range_points if start <= t <= end]


# 1. config parsing
assert cfg({}).session is None
b = cfg({"session": {"activity": "sum(increase(c[10m]))"}})
assert b.session == Session(activity="sum(increase(c[10m]))",
                            length=5 * H, align=H)
b = cfg({"session": {"activity": "a", "length": "3h", "align": "30m"}})
assert (b.session.length, b.session.align) == (3 * H, 1800.0)
for bad in ({}, {"activity": "a", "length": 0}, {"activity": "a", "align": -1}, "x"):
    try:
        cfg({"session": bad})
        raise AssertionError(f"accepted session: {bad!r}")
    except ConfigError:
        pass
print("1. session config parsing: OK")

# 2. anchor flooring
t = SessionTracker(Session(activity="a"))
assert t._floor(10 * H + 1234) == 10 * H
t = SessionTracker(Session(activity="a", align=0))
assert t._floor(12345.6) == 12345.6
print("2. anchor flooring: OK")

# 3. replay over history
now = 1_000_000 * H  # an exact hour, for easy arithmetic
tr = SessionTracker(Session(activity="a"))
assert tr._replay([], now) is None
# activity 90 minutes ago -> window still open, anchored on the hour
a = tr._replay([(now - 1.5 * H + 300, 2.0)], now)
assert a == tr._floor(now - 1.5 * H + 300) == now - 2 * H, a
# activity only 6h ago -> expired, no window
assert tr._replay([(now - 6 * H, 2.0)], now) is None
# 7h of continuous activity -> chained second window opens exactly at expiry
start = now - 7 * H
pts = [(start + i * 600, 1.0) for i in range(int(7 * H / 600))]
a = tr._replay(pts, now)
assert a == tr._floor(start) + 5 * H, (a - now) / H
# zeros and NaN never open a window
assert tr._replay([(now - H, 0.0), (now - H / 2, float("nan"))], now) is None
print("3. bootstrap replay (open, expired, chained, ignores 0/NaN): OK")

# 4. runtime state machine
s = Session(activity="act")
tr = SessionTracker(s)
prom = FakeProm(range_points=[], instant=[0.0])
assert tr.elapsed(prom, now) is None            # no history, no activity
prom.instant = [3.5]
e = tr.elapsed(prom, now + 100)                 # activity -> window opens
assert e == 100.0 and tr.anchor == now, (e, tr.anchor)
prom.instant = [0.0]
assert tr.elapsed(prom, now + 2 * H) == 2 * H   # no query while window open
assert tr.elapsed(prom, now + 5 * H + 1) is None  # expired -> closed
assert tr.anchor is None
print("4. tracker open/hold/expire cycle: OK")

# 5. ${session} formatting and MIN_WINDOW clamp
tr = SessionTracker(s)
tr._bootstrapped = True
tr.anchor = now
assert tr.window(FakeProm(), now + 30) == f"{int(MIN_WINDOW)}s"
assert tr.window(FakeProm(), now + 2 * H) == f"{int(2 * H)}s"
print("5. window() duration string + clamp: OK")

# 6. binding_value integration: substitution and no-session zero
b = cfg({"metric": "sum(increase(c[${session}]))/10",
         "session": {"activity": "act"}})
sessions = {}
prom = FakeProm(range_points=[(time.time() - 600, 1.0)], instant=[7.0])
value, status = binding_value(prom, b, {}, sessions)
assert status == "ok" and value == 7.0, (value, status)
sent = prom.queries[-1]
assert "${session}" not in sent and "[" in sent and sent.endswith("s]))/10"), sent
# closed window -> honest zero, metric never queried
prom2 = FakeProm(range_points=[], instant=[0.0])
value, status = binding_value(prom2, b, {}, {})
assert (value, status) == (0.0, "no-session")
assert all("increase(c[" not in q for q in prom2.queries)
print("6. binding_value substitutes ${session} / zero when closed: OK")

# 7. tracker rebuilt when the session config hot-reloads
first = sessions["s"]
value, status = binding_value(prom, b, {}, sessions)
assert sessions["s"] is first
b2 = cfg({"metric": "sum(increase(c[${session}]))/10",
          "session": {"activity": "act", "length": "3h"}})
binding_value(prom, b2, {}, sessions)
assert sessions["s"] is not first
print("7. tracker rebuilt on config change: OK")

print("\nall session regressions passed")
