"""Regression tests for the ten code-review fixes."""
import math
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml
from razerdash import webedit
from razerdash.backend import MockBackend
from razerdash.config import (ConfigError, ConfigWatcher, KeyGroup, Range,
                              load_config, parse_config)
from razerdash.daemon import Daemon
from razerdash.keygroup import KeyMap, KeyMapError, resolve, save_keymap
from razerdash.lighting import normalize
from razerdash.render import build_frame

TMP = tempfile.mkdtemp(prefix="razerdash-test-")
BASE = {"bindings": [{"name": "x", "metric": "up",
                      "key_group": {"type": "row", "index": 0},
                      "lighting": {"method": "fill_fixed", "color": "#00e5ff"}}]}


def _write(name, text):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# --- 1. malformed config values become ConfigError, incl. bad YAML ----------
for bad in ({"refresh_interval": "fast"}, {"background": "#zzz"},
            {"background": {"effect": "spectrum", "fps": "high"}},
            {"bindings": [{**BASE["bindings"][0], "priority": "high"}]}):
    try:
        parse_config({**BASE, **bad})
        raise AssertionError(f"accepted {bad}")
    except ConfigError:
        pass
try:
    load_config(_write("bad.yaml", "bindings: [\n  broken"))
    raise AssertionError("accepted bad YAML")
except ConfigError:
    pass
print("1. config parse errors -> ConfigError: OK")

# --- 2. corrupt keymap raises KeyMapError; daemon._keymap survives ----------
bad_yaml = _write("keymap-bad.yaml", "cells: [\n  broken")
bad_shape = _write("keymap-list.yaml", "- not\n- a\n- mapping\n")
bad_cells = _write("keymap-cells.yaml", "cells: [[0, 1], 5]\nnames: {}\n")
for p in (bad_yaml, bad_shape, bad_cells):
    try:
        KeyMap.load(p)
        raise AssertionError(f"loaded {p}")
    except KeyMapError:
        pass


d = Daemon.__new__(Daemon)
assert d._keymap(bad_yaml) is None  # warns, no raise
print("2. corrupt keymap -> KeyMapError, daemon degrades: OK")

# --- 3. draw failure in the main loop doesn't crash the daemon --------------
cfg_path = _write("config.yaml", yaml.safe_dump({
    "prometheus": {"url": "http://127.0.0.1:1", "timeout": "100ms"},
    "idle_interval": "50ms", **BASE}))


class _FailingDevice(MockBackend().find_device("x").__class__):
    def draw(self, frame, background=(0, 0, 0)):
        raise RuntimeError("device went away")


class _FailingBackend:
    def find_device(self, match):
        return _FailingDevice("gone", 8, 23)


w = ConfigWatcher(cfg_path)
w.load()
Daemon(w, _FailingBackend(), once=True).run()  # must return, not raise
print("3. main-loop draw failure -> survived: OK")

# --- 5/atomicity 6. save_keymap: correct content, atomic on failure ---------
km_path = os.path.join(TMP, "keymap.yaml")
save_keymap(km_path, [[0, 1]], {"a": [0, 1]})
assert KeyMap.load(km_path).names["a"] == (0, 1)
orig = open(km_path).read()
_real_dump = yaml.safe_dump
yaml.safe_dump = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    save_keymap(km_path, [[9, 9]], {})
    raise AssertionError("save should have failed")
except RuntimeError:
    pass
finally:
    yaml.safe_dump = _real_dump
assert open(km_path).read() == orig, "failed save clobbered the file"
assert not [f for f in os.listdir(TMP) if ".tmp." in f], "tmp file leaked"
print("6. atomic save_keymap: OK")

# --- 5. webedit loader raises ValueError with a message ---------------------
for p in (bad_yaml, bad_shape, bad_cells):
    try:
        webedit._load_keymap(p)
        raise AssertionError(f"webedit loaded {p}")
    except ValueError as e:
        assert str(e)
km, mtime = webedit._load_keymap(os.path.join(TMP, "nope.yaml"))
assert km == {"cells": [], "names": {}} and mtime is None
print("5. webedit malformed keymap -> ValueError: OK")

# --- 7. NaN treated as no-data --------------------------------------------
assert normalize(float("nan"), Range(0, 100)) == 0.0


class _NanProm:
    def query(self, q):
        return [float("nan")]


class _KG:
    pass


cfg = parse_config(dict(BASE))
frame, summary = build_frame(cfg, 8, 23, _NanProm())
assert frame == {} and summary[0][2] == "nan", summary
print("7. NaN metric -> empty frame, status 'nan': OK")

# --- 8. out-of-range / empty row and column indexes raise -------------------
for kg, ok in ((KeyGroup(type="row", index=7), True),
               (KeyGroup(type="row", index=8), False),
               (KeyGroup(type="row", index=-1), False),
               (KeyGroup(type="column", index=23), False)):
    try:
        resolve(kg, 8, 23, None)
        assert ok, f"accepted {kg}"
    except ValueError:
        assert not ok, f"rejected {kg}"
km = KeyMap(cells=[(0, 1), (0, 2)])
try:
    resolve(KeyGroup(type="row", index=3), 8, 23, km)
    raise AssertionError("empty keymap row accepted")
except ValueError:
    pass
print("8. index bounds validated: OK")

# --- 9. concurrent saves: exactly one wins, the other 409s ------------------
km_path2 = os.path.join(TMP, "keymap2.yaml")
save_keymap(km_path2, [[0, 1]], {})
# age the file: file mtimes use the kernel's coarse clock, so a write in the
# same tick as the original would be invisible to the mtime comparison
os.utime(km_path2, (os.stat(km_path2).st_atime, os.stat(km_path2).st_mtime - 5))
mt = os.stat(km_path2).st_mtime
ctx = {"lock": threading.Lock(), "path": km_path2}
results = []


def _try_save(tag):
    body = {"cells": [[0, int(tag)]], "names": {}, "mtime": mt}
    results.append(webedit._save(ctx, body)[0])


ts = [threading.Thread(target=_try_save, args=(i,)) for i in (1, 2)]
[t.start() for t in ts]
[t.join() for t in ts]
assert sorted(results) == [200, 409], results
print("9. concurrent save -> one 200, one 409: OK")

# --- 10. dims: mock backend and query defaults are 8x23 ---------------------
mb = MockBackend()
assert (mb.rows, mb.cols) == (8, 23)
import inspect
import razerdash.cli as c
src = inspect.getsource(c.main)
assert "default=8" in src and "default=23" in src
print("10. mock + query dims 8x23: OK")

print("\nall fix regressions passed")
