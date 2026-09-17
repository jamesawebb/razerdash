"""Compose all bindings into a single frame of (row, col) -> colour."""
from __future__ import annotations

from .context import expand, metric_context
from .keygroup import resolve
from .lighting import normalize, render_binding
from .prometheus import PrometheusError, reduce_values
from .session import SessionTracker


def binding_value(prom, b, ctx, sessions=None, logger=None):
    """Resolve one binding to (value, status).

    status "ok" means value is a usable float; otherwise value is None except
    for "no-session", where it is an honest 0.0 (the quota window is closed,
    nothing has been used) and the bar should render empty rather than skip.
    Session-windowed bindings get `${session}` added to the expansion context;
    the trackers live in `sessions` (name -> SessionTracker), which the caller
    keeps across polls so anchors persist.
    """
    ctx_b = ctx
    if b.session is not None and sessions is not None:
        tracker = sessions.get(b.name)
        if tracker is None or tracker.cfg != b.session:
            tracker = sessions[b.name] = SessionTracker(b.session)
        try:
            window = tracker.window(prom)
        except PrometheusError as e:
            if logger:
                logger.warning("[%s] session query failed: %s", b.name, e)
            return None, "query-error"
        if window is None:
            return 0.0, "no-session"
        ctx_b = dict(ctx, session=window)
    try:
        vals = prom.query(expand(b.metric, ctx_b))
    except PrometheusError as e:
        if logger:
            logger.warning("[%s] query failed: %s", b.name, e)
        return None, "query-error"
    value = reduce_values(vals, b.reduce)
    if value is None or value != value:
        # NaN (e.g. a 0/0 in the PromQL) means "no data", same as empty --
        # without this it would normalize to a full bar.
        if logger:
            logger.warning("[%s] %s result", b.name,
                           "empty" if value is None else "NaN")
        return None, "empty" if value is None else "nan"
    return value, "ok"


def build_frame(config, rows, cols, prom, keymap=None, logger=None, ctx=None,
                sessions=None, device=None):
    """Query every binding and paint it onto one frame.

    Bindings are drawn in ascending `priority`, so higher-priority bindings win
    on any keys that overlap. `device` limits the frame to bindings targeting
    that named device (None = all, for single-device callers). Returns
    (frame, summary) where summary is a list of (name, value|None, status)
    for logging.
    """
    if ctx is None:
        ctx = metric_context()
    frame: dict[tuple[int, int], tuple[int, int, int]] = {}
    summary = []
    for b in sorted(config.bindings, key=lambda x: x.priority):
        if device is not None and b.device != device:
            continue
        value, status = binding_value(prom, b, ctx, sessions, logger)
        if value is None:
            summary.append((b.name, None, status))
            continue
        try:
            keys = resolve(b.key_group, rows, cols, keymap)
            painted = render_binding(b, value, keys)
        except (KeyError, ValueError, IndexError, TypeError) as e:
            # e.g. a named-key group used before `razerdash calibrate` -> skip,
            # don't take the whole dashboard down. TypeError is the backstop
            # for a malformed group the config validation didn't foresee.
            if logger:
                logger.warning("[%s] key group unresolved (%s); skipping", b.name, e)
            summary.append((b.name, value, "no-keymap"))
            continue
        for (r, c, col) in painted:
            frame[(r, c)] = col
        summary.append((b.name, value,
                        status if status != "ok"
                        else f"{normalize(value, b.range) * 100:.0f}%"))
    return frame, summary
