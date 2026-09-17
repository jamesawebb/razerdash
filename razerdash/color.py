"""Colour parsing and interpolation helpers."""
from __future__ import annotations

import colorsys

RGB = "tuple[int, int, int]"


def parse_color(value) -> tuple[int, int, int]:
    """Accept "#rrggbb", "#rgb", or a [r, g, b] list -> (r, g, b) ints 0..255."""
    if value is None:
        return (0, 0, 0)
    if isinstance(value, (list, tuple)):
        vals = [int(x) for x in list(value)[:3]]
        while len(vals) < 3:
            vals.append(0)
        return tuple(max(0, min(255, v)) for v in vals)  # type: ignore[return-value]
    s = str(value).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"invalid colour: {value!r}")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def spectrum_color(t: float, period: float, saturation: float = 1.0,
                   brightness: float = 1.0) -> tuple[int, int, int]:
    """Colour of a spectrum-cycle effect at time `t` (seconds): one full trip
    around the hue wheel every `period` seconds."""
    hue = (t / period) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, saturation, brightness)
    return (round(r * 255), round(g * 255), round(b * 255))


def lerp(c1, c2, t: float) -> tuple[int, int, int]:
    """Linearly interpolate between two RGB colours, t in 0..1."""
    t = max(0.0, min(1.0, t))
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))  # type: ignore[return-value]


def threshold_color(stops, value):
    """Pick the colour of the highest stop whose `at` is <= value.

    `stops` is a value-sorted list of (at, colour). Below the first stop the
    first colour is used.
    """
    chosen = stops[0][1]
    for at, color in stops:
        if value >= at:
            chosen = color
        else:
            break
    return chosen


def gradient_color(stops, value):
    """Interpolate a colour across value-sorted (at, colour) stops."""
    if value <= stops[0][0]:
        return stops[0][1]
    if value >= stops[-1][0]:
        return stops[-1][1]
    for i in range(len(stops) - 1):
        a_at, a_c = stops[i]
        b_at, b_c = stops[i + 1]
        if a_at <= value <= b_at:
            span = b_at - a_at
            t = 0.0 if span == 0 else (value - a_at) / span
            return lerp(a_c, b_c, t)
    return stops[-1][1]
