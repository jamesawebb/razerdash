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
assert cfg.devices["mousepad"].write_gap is None     # => model default
assert cfg.devices["mousepad"].min_interval is None
cfg = parse_config({"devices": {"mousepad": {"match": "Firefly V2",
                                             "write_gap": "20ms",
                                             "min_interval": "5s"}},
                    "bindings": [{**PAD_BINDING}]})
assert cfg.devices["mousepad"].write_gap == 0.02
assert cfg.devices["mousepad"].min_interval == 5.0
print("2. devices mapping incl. shorthand + pacing: OK")

# --- 3. validation ------------------------------------------------------------
for bad in (
    {"devices": DEVICES, "keymap": "km.yaml", "bindings": [KB_BINDING]},  # mixed forms
    {"devices": {}, "bindings": [KB_BINDING]},                     # empty mapping
    {"devices": "Firefly V2", "bindings": [KB_BINDING]},           # wrong shape
    {"devices": {"pad": {}}, "bindings": [KB_BINDING]},            # no match
    {"devices": DEVICES, "bindings": [{**KB_BINDING, "device": "mouse"}]},  # unknown
    {"devices": {"keyboard": {"match": "x", "write_gap": "-1ms"}},
     "bindings": [KB_BINDING]},                                    # negative
    {"devices": {"keyboard": {"match": "x", "min_interval": "soon"}},
     "bindings": [KB_BINDING]},                                    # not a duration
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


# --- 10. identical frames are skipped; forget() and min_interval --------------
class _Counting(kb.__class__):
    def __init__(self, name, rows, cols):
        super().__init__(name, rows, cols)
        self.writes = []

    def _write(self, payload):
        self.writes.append(payload)


h = _Counting("kb", 2, 3)
f = {(0, 0): (255, 0, 0)}
assert h.draw(f, (1, 2, 3)) is True
assert h.draw(dict(f), (1, 2, 3)) is False          # identical: skipped
assert h.draw(f, (4, 5, 6)) is True                 # background changed
assert len(h.writes) == 2
h.forget()
assert h.draw(f, (4, 5, 6)) is True                 # forgotten: rewritten
h.pace(min_interval=60.0)
assert h.draw({}, (4, 5, 6)) is False               # changed, but capped
h._sent_at -= 61.0                                  # ... until the interval passes
assert h.draw({}, (4, 5, 6)) is True
assert len(h.writes) == 4
h.pace()                                            # back to the model default
assert h.min_interval == 0.0
print("10. identical frames skipped, forget() + min_interval: OK")


# --- 11. payload is exactly the bytes openrazer's Frame used to send ----------
h = _Counting("kb", 8, 23)
frame = {(0, 0): (255, 0, 0), (7, 22): (1, 2, 3), (3, 9): (0, 229, 255)}
h.draw(frame, (16, 16, 16))
try:
    from openrazer.client.fx import Frame
except ImportError:
    print("11. payload format: SKIPPED (no python3-openrazer)")
else:
    m = Frame((8, 23))
    for r in range(8):
        for c in range(23):
            m.set(r, c, frame.get((r, c), (16, 16, 16)))
    assert b"".join(h.writes[0]) == bytes(m)
    print("11. payload identical to openrazer's Frame: OK")


# --- 12. OpenRazerDevice: gaps, and stop writing once the device is gone ------
import time


class _Chroma:
    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail

    def setKeyRow(self, payload):
        if self.fail:
            raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")
        self.log.append(("row", bytes(payload), time.monotonic()))

    def setCustom(self):
        self.log.append(("apply", None, time.monotonic()))


class _FakePad:
    name = "Razer Firefly V2"
    serial = "PAD1"

    class fx:
        class advanced:
            rows, cols = 1, 19


calls = []
pad = backend_mod.OpenRazerDevice(_FakePad())
assert pad.gap == backend_mod.PACING["Firefly V2"][0] > 0   # model default
pad._chroma = _Chroma(calls)
backend_mod.OpenRazerDevice._last_io = time.monotonic()  # "the keyboard just wrote"
t0 = time.monotonic()
assert pad.draw({}, (9, 9, 9)) is True
assert [c[0] for c in calls] == ["row", "apply"]
assert calls[0][2] - t0 >= pad.gap * 0.95, "no gap after the previous write"
assert calls[1][2] - calls[0][2] >= pad.gap * 0.95, "no gap before the apply"

calls.clear()
kbd = backend_mod.OpenRazerDevice(_FakeDev())  # unpaced: one call for all rows
assert kbd.gap == 0.0
kbd._chroma = _Chroma(calls)
kbd.draw({}, (1, 1, 1))
assert [c[0] for c in calls] == ["row", "apply"]
assert len(calls[0][1]) == 8 * (3 + 23 * 3)

pad.pace(gap=0.0)                             # config override
assert pad.gap == 0.0
pad.pace()                                    # override removed on reload
assert pad.gap == backend_mod.PACING["Firefly V2"][0]

# A DBus error marks the handle gone: no further writes are attempted.
calls.clear()
kbd._chroma = _Chroma(calls, fail=True)
try:
    kbd.draw({}, (2, 2, 2))
    raise AssertionError("failed write did not raise")
except RuntimeError:
    pass
assert kbd.gone and not kbd.attached()
kbd._chroma = _Chroma(calls)
try:
    kbd.draw({}, (3, 3, 3))
    raise AssertionError("wrote to a gone device")
except backend_mod.DeviceGone:
    pass
assert calls == []

# A vanished HID interface (KVM switch, unplug) stops writes before openrazer
# notices; so does a re-enumeration (new node names).
hidroot = os.path.join(tmp, "hid")
nodes = [os.path.join(hidroot, f"0003:1532:0C04.000{i}") for i in (2, 3)]
for n in nodes:
    os.makedirs(n)
backend_mod.HID_SYSFS = hidroot
pad = backend_mod.OpenRazerDevice(_FakePad())
pad.pace(gap=0.0)
pad._chroma = _Chroma(calls)
pad._nodes = ((0x1532, 0x0C04), pad._hid_nodes(0x1532, 0x0C04))
assert pad.draw({}, (1, 1, 1)) is True and pad.attached()
os.rmdir(nodes[0])
try:
    pad.draw({}, (2, 2, 2))
    raise AssertionError("wrote to a device leaving the bus")
except backend_mod.DeviceGone:
    pass
assert len(calls) == 2  # only the first frame's row + apply
print("12. Firefly gaps; writes stop on DBus error or missing HID node: OK")


# --- 13. backend keeps handles across probes, re-finds a returning device -----
class _DBusDevices:
    def __init__(self):
        self.serials = ["KB1", "PAD1"]
        self.up = True

    def getDevices(self):
        if not self.up:
            raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")
        return list(self.serials)


class _Factory:
    @staticmethod
    def get_device(serial):
        return _FakeDev() if serial == "KB1" else _FakePad()


ob = backend_mod.OpenRazerBackend.__new__(backend_mod.OpenRazerBackend)
ob._devices = devs = _DBusDevices()
ob._dbus = None
ob._factory = _Factory
ob._handles = {}
ob._names = {"KB1": "Razer BlackWidow V4", "PAD1": "Razer Firefly V2"}
ob._no_matrix = set()

backend_mod.HID_SYSFS = os.path.join(tmp, "nohid")  # no presence check here
p1 = ob.find_device("Firefly V2")
assert p1 is not None and p1 is ob.find_device("Firefly V2")  # reused
assert ob.find_device("BlackWidow V4") is not p1
devs.serials = ["KB1"]                       # openrazer dropped it (KVM)
assert ob.find_device("Firefly V2") is None
devs.serials = ["KB1", "PAD1"]               # ... and reports it again
p2 = ob.find_device("Firefly V2")
assert p2 is not None and p2 is not p1       # fresh handle => full redraw
p2.gone = True                               # a write failed
p3 = ob.find_device("Firefly V2")
assert p3 is not p2
devs.up = False                              # openrazer-daemon went away
assert ob.find_device("Firefly V2") is None
assert ob._devices is None                   # reconnects on a later probe
print("13. backend reuses handles, re-finds a returning device: OK")


# --- 14. "drew" is INFO only when a written metric frame changed --------------
import logging

import razerdash.daemon as daemon_mod


class _CachedBackend:
    def __init__(self):
        self.devs = {"kb": _Counting("kb", 8, 23), "pad": _Counting("pad", 1, 19)}

    def find_device(self, match):
        return self.devs["pad" if "firefly" in match.lower() else "kb"]


class _LogCatcher(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))


lit = {"kb": {(0, 0): (0, 255, 0)}, "pad": {}}
real_build = daemon_mod.build_frame
daemon_mod.build_frame = lambda config, rows, cols, prom, keymap, log, ctx, sessions=None, device=None: (
    dict(lit["pad" if device == "mousepad" else "kb"]), [(device, 1.0, "ok")])
catcher = _LogCatcher()
rlog = logging.getLogger("razerdash")
rlog.addHandler(catcher)
old_level = rlog.level
rlog.setLevel(logging.DEBUG)
try:
    cb = _CachedBackend()
    d = Daemon(w, cb, once=True)

    def drew():
        catcher.records.clear()
        d.run()
        return [(lv, m) for lv, m in catcher.records
                if m.startswith(("drew", "unchanged"))]

    first = drew()
    assert [lv for lv, _ in first] == [logging.INFO], first
    assert "kb+pad" in first[0][1], first
    again = drew()                                     # same frames: DEBUG
    assert [lv for lv, _ in again] == [logging.DEBUG], again
    assert len(cb.devs["kb"].writes) == 1              # and nothing re-sent
    lit["kb"] = {(0, 0): (255, 0, 0)}                  # a bar changed colour
    changed = drew()
    assert [lv for lv, _ in changed] == [logging.INFO], changed
    assert "drew kb:" in changed[0][1], changed        # names what changed
    assert len(cb.devs["kb"].writes) == 2
    assert len(cb.devs["pad"].writes) == 1
finally:
    daemon_mod.build_frame = real_build
    rlog.removeHandler(catcher)
    rlog.setLevel(old_level)
print("14. 'drew' logged at INFO only on change, DEBUG otherwise: OK")


# --- 15. the animation tick absorbs settle gaps (frame rate unchanged) --------
from razerdash.config import Background


class _Settling(_Counting):
    def draw(self, frame, background=(0, 0, 0)):
        self.waited += 0.03  # as if two 15 ms gaps were slept inside the draw
        return super().draw(frame, background)


class _AnimCfg:
    background = Background(effect="spectrum", fps=10.0)


d = Daemon(w, _CachedBackend(), once=True)
d._paused = lambda: False
d._night = lambda config: None
slept = []
d._sleep = lambda dur: (slept.append(dur), time.sleep(dur))
d._animate({"pad": _Settling("pad", 1, 19)}, {"pad": {}}, _AnimCfg(), 0.35)
assert abs(slept[0] - 0.1) < 1e-6, slept            # first tick: full sleep
assert all(abs(s - 0.07) < 1e-6 for s in slept[1:-1]), slept  # minus 30 ms
print("15. animation tick absorbs settle gaps: OK")

print("\nall multi-device checks passed")
