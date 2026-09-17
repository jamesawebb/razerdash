"""Multi-device support: named devices, per-binding targeting, background sync."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from razerdash.backend import MockBackend
from razerdash.config import ConfigError, parse_config
from razerdash.render import build_frame

KB_BINDING = {"name": "kb", "metric": "up",
              "key_group": {"type": "row", "index": 0},
              "lighting": {"method": "fill_fixed", "color": "#00e5ff"}}
PAD_BINDING = {"name": "pad", "metric": "up", "device": "mousepad",
               "key_group": {"type": "row", "index": 0},
               "lighting": {"method": "fill_fixed", "color": "#ff00ff"}}
DEVICES = {"keyboard": {"match": "BlackWidow V4", "keymap": "keymap.yaml"},
           "mousepad": "Firefly V2"}

# --- 1. historical single-device form -> one 'keyboard' device --------------
cfg = parse_config({"device": {"match": "BlackWidow V4"}, "keymap": "km.yaml",
                    "bindings": [KB_BINDING]})
assert list(cfg.devices) == ["keyboard"]
assert cfg.primary == "keyboard"
assert cfg.device.match == "BlackWidow V4"      # back-compat property
assert cfg.keymap.endswith("km.yaml")           # back-compat property
assert cfg.bindings[0].device == "keyboard"     # default target = primary
print("1. single-device back-compat: OK")

# --- 2. devices mapping: dict + string shorthand, per-device keymap ----------
cfg = parse_config({"devices": DEVICES, "bindings": [KB_BINDING, PAD_BINDING]})
assert list(cfg.devices) == ["keyboard", "mousepad"]
assert cfg.devices["keyboard"].keymap.endswith("keymap.yaml")
assert cfg.devices["mousepad"].match == "Firefly V2"
assert cfg.devices["mousepad"].keymap is None
assert [b.device for b in cfg.bindings] == ["keyboard", "mousepad"]
print("2. devices mapping incl. shorthand: OK")

# --- 3. validation ------------------------------------------------------------
for bad in (
    {"devices": DEVICES, "keymap": "km.yaml", "bindings": [KB_BINDING]},  # mixed forms
    {"devices": {}, "bindings": [KB_BINDING]},                     # empty mapping
    {"devices": "Firefly V2", "bindings": [KB_BINDING]},           # wrong shape
    {"devices": {"pad": {}}, "bindings": [KB_BINDING]},            # no match
    {"devices": DEVICES, "bindings": [{**KB_BINDING, "device": "mouse"}]},  # unknown
):
    try:
        parse_config(bad)
        raise AssertionError(f"accepted {bad}")
    except ConfigError:
        pass
print("3. device validation errors: OK")

# --- 4. duplicate index check is per-device ----------------------------------
cfg = parse_config({"devices": DEVICES, "bindings": [KB_BINDING, PAD_BINDING]})
assert len(cfg.bindings) == 2  # same row 0 on different devices is fine
try:
    parse_config({"devices": DEVICES,
                  "bindings": [PAD_BINDING, {**PAD_BINDING, "name": "pad2"}]})
    raise AssertionError("accepted duplicate row on one device")
except ConfigError:
    pass
print("4. duplicate indexes scoped to a device: OK")


# --- 5. build_frame paints only the named device's bindings -------------------
class _Prom:
    def query(self, q):
        return [100.0]


cfg = parse_config({"devices": DEVICES, "bindings": [KB_BINDING, PAD_BINDING]})
kb_frame, kb_summary = build_frame(cfg, 8, 23, _Prom(), device="keyboard")
pad_frame, pad_summary = build_frame(cfg, 1, 19, _Prom(), device="mousepad")
assert set(kb_frame) == {(0, c) for c in range(23)}
assert kb_frame[(0, 0)] == (0, 229, 255)
assert set(pad_frame) == {(0, c) for c in range(19)}
assert pad_frame[(0, 0)] == (255, 0, 255)
assert [s[0] for s in kb_summary] == ["kb"]
assert [s[0] for s in pad_summary] == ["pad"]
print("5. per-device frames: OK")

# --- 6. mock backend knows the Firefly's 1x19 shape ---------------------------
mb = MockBackend()
kb = mb.find_device("BlackWidow V4")
pad = mb.find_device("Firefly V2")
assert (kb.rows, kb.cols) == (8, 23)
assert (pad.rows, pad.cols) == (1, 19)
print("6. mock Firefly profile 1x19: OK")


# --- 7. daemon draws every attached device with one background colour ---------
class _Recorder(kb.__class__):
    def __init__(self, name, rows, cols, log):
        super().__init__(name, rows, cols)
        self._log = log

    def draw(self, frame, background=(0, 0, 0)):
        self._log.append((self.name, self.rows, self.cols, background))


class _RecordingBackend:
    def __init__(self):
        self.draws = []

    def find_device(self, match):
        if "firefly" in match.lower():
            return _Recorder("pad", 1, 19, self.draws)
        return _Recorder("kb", 8, 23, self.draws)


import tempfile

import yaml
from razerdash.config import ConfigWatcher
from razerdash.daemon import Daemon

tmp = tempfile.mkdtemp(prefix="razerdash-test-")
cfg_path = os.path.join(tmp, "config.yaml")
with open(cfg_path, "w", encoding="utf-8") as f:
    yaml.safe_dump({
        "prometheus": {"url": "http://127.0.0.1:1", "timeout": "100ms"},
        "idle_interval": "50ms",
        "background": "#101010",
        "devices": {"keyboard": "BlackWidow V4", "mousepad": "Firefly V2"},
        "bindings": [KB_BINDING, PAD_BINDING],
    }, f)
w = ConfigWatcher(cfg_path)
w.load()
backend = _RecordingBackend()
Daemon(w, backend, once=True).run()
assert len(backend.draws) == 2, backend.draws
names = {d[0] for d in backend.draws}
assert names == {"kb", "pad"}, backend.draws
bgs = {d[3] for d in backend.draws}
assert bgs == {(16, 16, 16)}, "devices drew different backgrounds"
print("7. daemon draws all devices, one shared background: OK")


# --- 8. driver-mode guard: re-asserts 0x03 via the razerkbd sysfs node --------
import razerdash.backend as backend_mod


class _Adv:
    rows, cols = 8, 23


class _Fx:
    advanced = _Adv()


class _FakeDev:
    name = "Razer BlackWidow V4"
    serial = "XY123"
    fx = _Fx()


sysroot = os.path.join(tmp, "razerkbd")
node = os.path.join(sysroot, "0003:1532:0287.0001")
os.makedirs(node)
with open(os.path.join(node, "device_serial"), "w") as f:
    f.write("XY123\n")
mode_path = os.path.join(node, "device_mode")
with open(mode_path, "wb") as f:
    f.write(b"\x00\x00")  # device mode: the KVM-switch blackout state

backend_mod.RAZERKBD_SYSFS = sysroot
d = backend_mod.OpenRazerDevice(_FakeDev())
assert d.ensure_driver_mode() == "fixed"
with open(mode_path, "rb") as f:
    assert f.read() == b"\x03\x00"
assert d.ensure_driver_mode() is None  # already in driver mode: nothing to do

other = backend_mod.OpenRazerDevice(_FakeDev())
other._serial = "UNKNOWN"  # no razerkbd node for it (e.g. the Firefly)
assert other.ensure_driver_mode() is None
print("8. driver-mode guard fixes device mode via sysfs: OK")


# --- 9. daemon checks driver mode on every probe, before drawing --------------
class _ModeRecorder(kb.__class__):
    def __init__(self, name, rows, cols, log, fail=False):
        super().__init__(name, rows, cols)
        self._log = log
        self._fail = fail

    def ensure_driver_mode(self):
        self._log.append((self.name, "ensure"))
        if self._fail:
            raise OSError("permission denied")
        return "fixed"

    def draw(self, frame, background=(0, 0, 0)):
        self._log.append((self.name, "draw"))


class _ModeBackend:
    def __init__(self):
        self.events = []

    def find_device(self, match):
        if "firefly" in match.lower():
            return _ModeRecorder("pad", 1, 19, self.events, fail=True)
        return _ModeRecorder("kb", 8, 23, self.events)


backend = _ModeBackend()
Daemon(w, backend, once=True).run()
ev = backend.events
for n in ("kb", "pad"):
    assert ev.count((n, "ensure")) == 1, ev
    assert ev.count((n, "draw")) == 1, ev  # OSError from ensure doesn't stop draws
    assert ev.index((n, "ensure")) < ev.index((n, "draw")), ev
print("9. daemon runs the driver-mode guard each probe: OK")

print("\nall multi-device checks passed")
