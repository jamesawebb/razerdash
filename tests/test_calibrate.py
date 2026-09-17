"""calibrate edit-in-place: prompted defaults, '-' to clear, name moves."""
import builtins
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from razerdash import calibrate
from razerdash.keygroup import KeyMap, save_keymap


class _Dev:
    name = "Fake Firefly"
    rows, cols = 1, 5

    def draw(self, frame, background=(0, 0, 0)):
        pass

    def clear(self):
        pass


tmp = tempfile.mkdtemp(prefix="razerdash-test-")
out = os.path.join(tmp, "pad.yaml")


def run(answers):
    prompts, it = [], iter(answers)
    real_input = builtins.input

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(it)

    builtins.input = fake_input
    try:
        calibrate._calibrate(_Dev(), types.SimpleNamespace(output=out))
    finally:
        builtins.input = real_input
    return KeyMap.load(out), prompts


# --- 1. fresh run: no existing file, Enter skips, qqq saves -------------------
km, _ = run(["z0", "z1", "qqq"])
assert km.cells == {(0, 0), (0, 1)}
assert km.names == {"z0": (0, 0), "z1": (0, 1)}
print("1. fresh calibration: OK")

# --- 2. edit in place: keep (Enter), rename, clear ('-'), move a name ---------
# Existing file mirrors the real-world case: an orphan cell (0,3) that is in
# `cells` but has no name (the old duplicate-name overwrite could produce it).
save_keymap(out, [[0, 0], [0, 1], [0, 2], [0, 3]],
            {"a": [0, 0], "b": [0, 1], "c": [0, 2]})
km, prompts = run(["",     # (0,0): keep 'a'... until it moves below
                   "x",    # (0,1): rename b -> x
                   "-",    # (0,2): clear the cell entirely
                   "",     # (0,3): keep the unnamed cell
                   "a"])   # (0,4): 'a' moves here from (0,0)
assert "name [a] > " in prompts[0], prompts[0]  # current name as default
assert "name [b] > " in prompts[1], prompts[1]
assert prompts[3].endswith("name > "), prompts[3]  # unnamed cell: no hint
assert km.cells == {(0, 0), (0, 1), (0, 3), (0, 4)}, km.cells
assert km.names == {"x": (0, 1), "a": (0, 4)}, km.names
print("2. edit-in-place keep/rename/clear/move: OK")

# --- 3. early qqq keeps every unvisited entry ---------------------------------
km2, _ = run(["qqq"])
assert km2.cells == km.cells and km2.names == km.names
print("3. early qqq preserves unvisited entries: OK")

print("\nall calibrate checks passed")
