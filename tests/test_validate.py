"""Config validation: key_group / reduce shapes that used to parse cleanly and
then crash the render loop with a TypeError, plus the lit_count rounding fix."""
import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from razerdash.config import ConfigError, KeyGroup, Range, parse_config
from razerdash.lighting import lit_count
from razerdash.render import build_frame

LIGHT = {"method": "fill_fixed", "color": "#00e5ff"}


def cfg_with(**binding):
    b = {"name": "x", "metric": "up", "lighting": LIGHT,
         "key_group": {"type": "row", "index": 0}}
    b.update(binding)
    return {"bindings": [b]}


def rejects(raw, needle):
    try:
        parse_config(raw)
    except ConfigError as e:
        assert needle in str(e), f"wrong error for {raw}: {e}"
        return
    raise AssertionError(f"accepted {raw}")


class _Prom:
    def query(self, q):
        return [42.0]


# --- 1. key_group.index is coerced to int, or rejected -----------------------
for raw, want in (("3", 3), (3.0, 3), (3, 3)):
    kg = parse_config(cfg_with(key_group={"type": "row", "index": raw})).bindings[0].key_group
    assert kg.index == want and type(kg.index) is int, (raw, kg.index)
for bad in (3.5, "three", True, [3], float("nan")):
    rejects(cfg_with(key_group={"type": "row", "index": bad}), "index")
print("1. key_group.index coerced to int / rejected: OK")

# --- 2. key_group.keys entries are names or [row, col]; bare ints rejected ---
kg = parse_config(cfg_with(key_group={"type": "keys",
                                      "keys": ["m1", [0, 1], ["0", 2.0]]})).bindings[0].key_group
assert kg.keys == ["m1", (0, 1), (0, 2)], kg.keys
for bad in ([5, 6], [[0]], [[0, 1, 2]], [None], [[0, 1.5]], "m1"):
    rejects(cfg_with(key_group={"type": "keys", "keys": bad}), "keys")
print("2. key_group.keys entries validated: OK")

# --- 3. order / reduce are whitelisted (typos used to be silent defaults) ----
for order in ("left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top"):
    parse_config(cfg_with(key_group={"type": "row", "index": 0, "order": order}))
rejects(cfg_with(key_group={"type": "row", "index": 0, "order": "rtl"}), "order")
for mode in ("first", "last", "max", "min", "sum", "avg"):
    assert parse_config(cfg_with(reduce=mode)).bindings[0].reduce == mode
rejects(cfg_with(reduce="maximum"), "reduce")
print("3. order / reduce whitelisted: OK")

# --- 4. duplicate binding names rejected -------------------------------------
raw = cfg_with()
raw["bindings"].append({**raw["bindings"][0], "key_group": {"type": "row", "index": 1}})
rejects(raw, "duplicate binding name")
print("4. duplicate binding names rejected: OK")

# --- 5. named key_groups go through the same validation ----------------------
rejects({"key_groups": {"g": {"type": "keys", "keys": [5, 6]}}, **cfg_with(key_group="g")},
        "keys")
print("5. named key_groups validated: OK")

# --- 6. the render loop survives a TypeError from a hand-built bad group -----
cfg = parse_config(cfg_with())
cfg.bindings[0].key_group = KeyGroup(type="keys", keys=[5, 6])  # bypasses parsing
frame, summary = build_frame(cfg, 8, 23, _Prom())
assert frame == {} and summary[0][2] == "no-keymap", summary
print("6. build_frame survives a TypeError (backstop): OK")

# --- 7. lit_count rounds half-up, so a 5-key bar steps evenly ----------------
class _B:
    range = Range(0, 100)

got = [lit_count(_B, v, 5) for v in range(0, 101, 10)]
assert got == [0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5], got
assert lit_count(_B, 5, 10) == 1 and lit_count(_B, 4, 10) == 0   # .5 -> up, .4 -> down
assert lit_count(_B, float("nan"), 5) == 0
assert lit_count(_B, 250, 5) == 5                                # clamped
print("7. lit_count half-up rounding: OK")

print("\nall validation checks passed")
