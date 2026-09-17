"""Render a plain-English description of the active configuration.

`describe_config` turns a parsed Config into ASCII prose (no Prometheus needed);
pass `values` to also show each binding's current reading. Output is ASCII-only
so it prints cleanly regardless of the terminal locale.
"""
from __future__ import annotations

from .context import expand, metric_context
from .keygroup import resolve
from .lighting import lit_count, normalize

# Basic named colours for a nearest-match hint; the exact hex is always shown.
_NAMED = {
    "red": (255, 0, 0), "green": (0, 255, 0), "spring green": (0, 255, 136),
    "blue": (0, 0, 255), "yellow": (255, 255, 0), "amber": (255, 170, 0),
    "orange": (255, 140, 0), "cyan": (0, 229, 255), "magenta": (255, 0, 255),
    "white": (255, 255, 255), "black": (0, 0, 0),
}


def color_name(rgb) -> str:
    r, g, b = rgb
    best, bd = "", None
    for name, (nr, ng, nb) in _NAMED.items():
        d = (r - nr) ** 2 + (g - ng) ** 2 + (b - nb) ** 2
        if bd is None or d < bd:
            best, bd = name, d
    return best


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(x) for x in rgb)


def _swatch(rgb) -> str:
    return f"{color_name(rgb)} ({_hex(rgb)})"


def _num(x) -> str:
    f = float(x)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _threshold_bands(stops) -> str:
    parts = []
    n = len(stops)
    for i, (at, rgb) in enumerate(stops):
        sw = _swatch(rgb)
        if n == 1:
            parts.append(f"{sw} throughout")
        elif i == n - 1:
            parts.append(f"{sw} at/above {_num(at)}")
        elif i == 0 and float(at) == 0:
            parts.append(f"{sw} below {_num(stops[1][0])}")
        else:
            parts.append(f"{sw} {_num(at)}-{_num(stops[i + 1][0])}")
    return ", ".join(parts)


def _gradient_stops(stops) -> str:
    return " -> ".join(f"{_swatch(rgb)} at {_num(at)}" for at, rgb in stops)


def _group_label(kg) -> str:
    if kg.type == "row":
        return f"Row {kg.index}"
    if kg.type == "column":
        return f"Col {kg.index}"
    return "Keys"


def _key_count(kg, keymap, rows, cols):
    try:
        return len(resolve(kg, rows, cols, keymap))
    except Exception:
        return None


def _shows(b, keymap, rows, cols):
    lt, kg, rng = b.lighting, b.key_group, b.range
    span = f"{_num(rng.min)}-{_num(rng.max)}"
    n = _key_count(kg, keymap, rows, cols)
    nkeys = f"{n} keys" if n is not None else "its keys (needs calibration)"
    lines = []
    if kg.type == "keys":
        lines.append("group:  " + ", ".join(str(k) for k in (kg.keys or [])))
    if b.session is not None:
        hours = b.session.length / 3600
        lines.append(f"window: usage since the current {_num(hours)}h session "
                     f"opened (anchored to the hour); zero between sessions")
    m = lt.method
    if m == "fill_threshold":
        lines.append(f"shows:  a bar across {nkeys}; more keys light as the value rises ({span})")
        lines.append(f"colour: {_threshold_bands(lt.thresholds)}")
    elif m == "fill_fixed":
        lines.append(f"shows:  a {_swatch(lt.color)} bar across {nkeys}; more keys "
                     f"light as the value rises ({span})")
    elif m == "solid_threshold":
        lines.append(f"shows:  all {nkeys} one colour, chosen by level")
        lines.append(f"colour: {_threshold_bands(lt.thresholds)}")
    elif m == "gradient":
        lines.append(f"shows:  all {nkeys} one colour, blended across the range ({span})")
        lines.append(f"colour: {_gradient_stops(lt.stops)}")
    else:
        lines.append(f"shows:  {m}")
    if kg.order in ("right_to_left", "bottom_to_top"):
        lines.append(f"fill:   {kg.order.replace('_', ' ')}")
    if lt.off_color is not None:
        lines.append(f"unlit:  {_swatch(lt.off_color)} (overrides the global background)")
    return lines


def _now_line(b, values, keymap, rows, cols):
    if not values or b.name not in values:
        return None
    v = values[b.name]
    if isinstance(v, str):
        return f"now:    {v}"
    if v is None:
        return "now:    (no data)"
    pct = normalize(v, b.range) * 100
    n = _key_count(b.key_group, keymap, rows, cols)
    if b.lighting.method in ("fill_threshold", "fill_fixed") and n:
        return f"now:    {v:.1f}  ({pct:.0f}%)  ->  {lit_count(b, v, n)}/{n} keys lit"
    return f"now:    {v:.1f}  ({pct:.0f}%)"


def _bg_line(bg) -> str:
    if bg.animated:
        return (f"Background: spectrum -- keys not lit by a binding cycle through"
                f" the hues, one lap per {_num(bg.period)}s, redrawn at"
                f" {_num(bg.fps)} fps (brightness {_num(bg.brightness * 100)}%)")
    c = tuple(int(x) for x in bg.color)
    if c == (0, 0, 0):
        return "Background: off -- keys not lit by a binding stay dark"
    if max(c) < 40:
        return f"Background: a dim wash ({_hex(c)}) on every key not lit by a binding"
    return f"Background: {_swatch(c)} on every key not lit by a binding"


def _sleep_line(sl) -> str:
    parts = []
    if sl.follow_sway:
        parts.append("LEDs go dark while all monitors are off (sway)")
    if sl.idle_timeout > 0:
        parts.append(f"dark after {_num(sl.idle_timeout)}s without input")
    if not parts:
        return "Sleep:      never -- LEDs stay on around the clock"
    return "Sleep:      " + "; ".join(parts)


def _keymap_note(path, keymap) -> str:
    if not path:
        return "no keymap -- row/column bars span the full matrix"
    if keymap is None:
        return f"keymap {path} (NOT loaded -- named-key groups will be skipped)"
    return f"keymap {path} ({len(keymap.cells)} keys mapped)"


def describe_config(config, keymaps=None, dims=None, values=None) -> str:
    """`keymaps` and `dims` are per-device-name mappings (see cli._device_views);
    devices they don't cover fall back to no keymap and an 8x23 matrix."""
    keymaps = keymaps or {}
    dims = dims or {}
    ctx = metric_context()
    p = config.prometheus
    multi = len(config.devices) > 1
    out = []
    for i, (name, d) in enumerate(config.devices.items()):
        head = "Devices:   " if multi else "Device:    "
        r, c = dims.get(name, (8, 23))
        out.append(f"{head if i == 0 else '           '} {name}: matching "
                   f"'{d.match}', {r}x{c}, {_keymap_note(d.keymap, keymaps.get(name))}")
    out += [
        f"Prometheus: {p.url}   refresh: every {_num(config.refresh_interval)}s"
        f"   idle poll: every {_num(config.idle_interval)}s",
        _bg_line(config.background),
        _sleep_line(config.sleep),
        "",
    ]
    for b in config.bindings:
        label = b.label or b.name
        keymap = keymaps.get(b.device)
        rows, cols = dims.get(b.device, (8, 23))
        where = _group_label(b.key_group)
        if multi:
            where += f" of {b.device}"
        out.append(f"{where}   {label}  ({b.name})")
        out.append(f"    metric: {expand(b.metric, ctx)}")
        out.extend("    " + ln for ln in _shows(b, keymap, rows, cols))
        now = _now_line(b, values, keymap, rows, cols)
        if now:
            out.append("    " + now)
        out.append("")
    return "\n".join(out).rstrip() + "\n"
