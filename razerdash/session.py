"""Anchored usage windows: mirror quota schemes like Claude's 5-hour session.

A sliding `increase(counter[5h])` keeps counting old usage for up to five
hours after the provider's quota window has already reset. Real session
semantics are: the window OPENS at the first activity after the previous
window expired (anchored to the top of the hour -- that is how Claude's
`/usage` reset times behave), lasts `length`, then usage drops to zero until
the next activity opens a new window.

That anchor cannot be expressed in PromQL, so the daemon tracks it here:

- bootstrap: replay the `activity` query over recent history (a range query)
  to find the session already in progress when the daemon starts;
- while a window is open: no extra queries -- the binding's metric is
  rendered with `${session}` = time since the anchor, so
  `increase(counter[${session}])` is exactly "usage this session";
- when the window expires: the value reports as "no session" (zero) and each
  poll runs the cheap instant `activity` query until fresh usage opens a new
  window anchored at the current hour.

Approximations, all bounded by the hour alignment: the bootstrap replay sees
activity in REPLAY_STEP buckets, so an anchor can be off when a session
starts in the last minutes before an hour mark; and if usage runs past a
window's expiry, the next window opens immediately rather than at the next
message. Both self-correct at the next real gap in usage.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger("razerdash")

REPLAY_STEP = 600.0      # bootstrap resolution; `activity` should use ~[10m]
REPLAY_LOOKBACK = 2.5    # in units of cfg.length
MIN_WINDOW = 120.0       # floor for ${session}: survive scrape/export lag


class SessionTracker:
    """Tracks the current window anchor for one binding's `session:` block."""

    def __init__(self, cfg):
        self.cfg = cfg                 # config.Session
        self.anchor: Optional[float] = None  # unix time the window opened
        self._bootstrapped = False

    def _floor(self, t: float) -> float:
        a = self.cfg.align
        return t - (t % a) if a > 0 else t

    def _expire(self, now: float):
        if self.anchor is not None and now >= self.anchor + self.cfg.length:
            opened = time.strftime("%H:%M", time.localtime(self.anchor))
            self.anchor = None
            log.info("session window over (opened %s); usage resets", opened)

    def _replay(self, points, now: float) -> Optional[float]:
        """Apply the window rule over historical activity points."""
        anchor = None
        for t, v in points:
            if v != v or v <= 0:
                continue
            # An increase() bucket at t covers (t-step, t]. Flooring t keeps
            # back-to-back windows exact (a chained window opens exactly at
            # the previous expiry); the cost is that a session whose first
            # message straddles the hour before its bucket may anchor one
            # hour late until the next real gap.
            if anchor is None or t >= anchor + self.cfg.length:
                anchor = self._floor(t)
        if anchor is not None and now >= anchor + self.cfg.length:
            anchor = None
        return anchor

    def bootstrap(self, prom, now: float):
        points = prom.query_range(self.cfg.activity,
                                  now - REPLAY_LOOKBACK * self.cfg.length,
                                  now, REPLAY_STEP)
        self.anchor = self._replay(points, now)
        self._bootstrapped = True
        if self.anchor is not None:
            log.info("session window in progress since %s (resets %s)",
                     time.strftime("%H:%M", time.localtime(self.anchor)),
                     time.strftime("%H:%M", time.localtime(
                         self.anchor + self.cfg.length)))

    def elapsed(self, prom, now: Optional[float] = None) -> Optional[float]:
        """Seconds since the current window opened, or None while no window
        is active. Raises PrometheusError if a needed query fails."""
        if now is None:
            now = time.time()
        if not self._bootstrapped:
            self.bootstrap(prom, now)
        self._expire(now)
        if self.anchor is None:
            vals = prom.query(self.cfg.activity)
            if not any(v > 0 for v in vals if v == v):
                return None
            self.anchor = self._floor(now)
            log.info("new session window opened at %s (resets %s)",
                     time.strftime("%H:%M", time.localtime(self.anchor)),
                     time.strftime("%H:%M", time.localtime(
                         self.anchor + self.cfg.length)))
        return now - self.anchor

    def window(self, prom, now: Optional[float] = None) -> Optional[str]:
        """The `${session}` substitution: elapsed time as a PromQL duration,
        or None while no window is active."""
        e = self.elapsed(prom, now)
        if e is None:
            return None
        return f"{int(max(e, MIN_WINDOW))}s"
