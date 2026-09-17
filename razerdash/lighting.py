"""The lighting methods: turn a metric value into per-key colours."""
from __future__ import annotations

import math

from .color import gradient_color, threshold_color


def normalize(value, rng) -> float:
    """Map value from [rng.min, rng.max] to 0..1, clamped. NaN maps to 0.0 --
    min/max don't order NaN, so clamping alone would let it through as 1.0."""
    if rng.max == rng.min:
        return 0.0
    t = (value - rng.min) / (rng.max - rng.min)
    if t != t:
        return 0.0
    return max(0.0, min(1.0, t))


def lit_count(binding, value, n: int) -> int:
    """Keys to light: the normalised fraction of `n`, rounded half-up.
    Python's round() is half-to-even, which on a 5-key bar gave 10% -> 0
    keys but 30% -> 2, 50% -> 2, 70% -> 4 -- an unevenly stepping bar."""
    return int(math.floor(normalize(value, binding.range) * n + 0.5))


def render_binding(binding, value, group_keys):
    """Return [(row, col, (r, g, b)), ...] for one binding at `value`."""
    lt = binding.lighting
    method = lt.method
    out = []

    if method in ("fill_threshold", "fill_fixed"):
        lit = lit_count(binding, value, len(group_keys))
        on = threshold_color(lt.thresholds, value) if method == "fill_threshold" else lt.color
        for i, (r, c) in enumerate(group_keys):
            if i < lit:
                out.append((r, c, on))
            elif lt.off_color is not None:
                # Explicit per-binding "off" colour. When unset, the unlit part
                # of the bar is left to the global background at draw time.
                out.append((r, c, lt.off_color))
    elif method == "solid_threshold":
        color = threshold_color(lt.thresholds, value)
        out = [(r, c, color) for (r, c) in group_keys]
    elif method == "gradient":
        color = gradient_color(lt.stops, value)
        out = [(r, c, color) for (r, c) in group_keys]

    return out
