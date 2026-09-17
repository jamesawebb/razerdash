"""Sanity checks for the animated-background config plumbing."""
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from razerdash.config import parse_config, ConfigError
from razerdash.explain import _bg_line

BASE = {
    "bindings": [{
        "name": "x", "metric": "up",
        "key_group": {"type": "row", "index": 0},
        "lighting": {"method": "fill_fixed", "color": "#00e5ff"},
    }],
}

# 1. historical scalar form stays static
cfg = parse_config({**BASE, "background": "#0a0a12"})
assert not cfg.background.animated
assert cfg.background.color == (10, 10, 18)
assert cfg.background.color_at(123.4) == (10, 10, 18)

# 2. absent -> static black
cfg = parse_config(dict(BASE))
assert cfg.background.color_at(5) == (0, 0, 0)

# 3. spectrum mapping, duration string, custom fps
cfg = parse_config({**BASE, "background": {
    "effect": "spectrum", "fps": 4, "period": "12s", "brightness": 1.0}})
bg = cfg.background
assert bg.animated and bg.fps == 4 and bg.period == 12.0

# 4. hue actually cycles: red -> green -> blue thirds, and wraps
r0, g0, b0 = bg.color_at(0)
assert (r0, g0, b0) == (255, 0, 0), bg.color_at(0)
assert bg.color_at(4) == (0, 255, 0), bg.color_at(4)      # period/3
assert bg.color_at(8) == (0, 0, 255), bg.color_at(8)      # 2*period/3
assert bg.color_at(12) == bg.color_at(0)                  # wraps
assert bg.color_at(2) == (255, 255, 0)                    # yellow between

# 5. default brightness dims (0.4 -> max channel 102)
cfg = parse_config({**BASE, "background": {"effect": "spectrum"}})
assert max(cfg.background.color_at(0)) == 102, cfg.background.color_at(0)
assert cfg.background.fps == 10.0 and cfg.background.period == 30.0

# 6. validation errors
for bad in (
    {"effect": "wave"},
    {"effect": "spectrum", "fps": 0},
    {"effect": "spectrum", "fps": 120},
    {"effect": "spectrum", "period": "0s"},
    {"effect": "spectrum", "brightness": 1.5},
    {"effect": "spectrum", "saturation": -0.1},
):
    try:
        parse_config({**BASE, "background": bad})
    except ConfigError as e:
        pass
    else:
        raise AssertionError(f"accepted invalid background {bad}")

# 7. explain lines for both forms
line = _bg_line(parse_config({**BASE, "background": {"effect": "spectrum"}}).background)
assert "spectrum" in line and "30s" in line and "10 fps" in line, line
print("spectrum:", line)
line = _bg_line(parse_config({**BASE, "background": "#0a0a12"}).background)
assert "dim wash" in line, line
print("static:  ", line)

print("all background checks passed")
